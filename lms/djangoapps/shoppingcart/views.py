import logging
import datetime
import decimal
import pytz
from django.db.models import Q
from django.conf import settings
from django.contrib.auth.models import Group
from django.contrib.auth import get_user
from django.http import (
    HttpResponse, HttpResponseRedirect, HttpResponseNotFound,
    HttpResponseBadRequest, HttpResponseForbidden, Http404
)
from django.utils.translation import ugettext as _
from course_modes.models import CourseMode
from util.json_request import JsonResponse
from django.views.decorators.http import require_POST, require_http_methods
from django.core.urlresolvers import reverse
from django.views.decorators.csrf import csrf_exempt
from util.bad_request_rate_limiter import BadRequestRateLimiter
from util.date_utils import get_default_time_display
from django.contrib.auth.decorators import login_required
from microsite_configuration import microsite
from edxmako.shortcuts import render_to_response
from opaque_keys.edx.locations import SlashSeparatedCourseKey
from opaque_keys.edx.locator import CourseLocator
from opaque_keys import InvalidKeyError
from courseware.courses import get_course_by_id
from courseware.views import registered_for_course
from config_models.decorators import require_config
from shoppingcart.reports import RefundReport, ItemizedPurchaseReport, UniversityRevenueShareReport, CertificateStatusReport
from student.models import CourseEnrollment, EnrollmentClosedError, CourseFullError, \
    AlreadyEnrolledError
from .exceptions import (
    ItemAlreadyInCartException, AlreadyEnrolledInCourseException,
    CourseDoesNotExistException, ReportTypeDoesNotExistException,
    MultipleCouponsNotAllowedException, InvalidCartItem,
    ItemNotFoundInCartException, RedemptionCodeError
)
from .models import (
    Order, OrderTypes,
    PaidCourseRegistration, OrderItem, Coupon,
    CouponRedemption, CourseRegistrationCode, RegistrationCodeRedemption,
    CourseRegCodeItem, Donation, DonationConfiguration
)
from .processors import (
    process_postpay_callback, render_purchase_form_html,
    get_signed_purchase_params, get_purchase_endpoint
)

import json
from xmodule_django.models import CourseKeyField
from .decorators import enforce_shopping_cart_enabled

log = logging.getLogger("shoppingcart")
AUDIT_LOG = logging.getLogger("audit")

EVENT_NAME_USER_UPGRADED = 'edx.course.enrollment.upgrade.succeeded'

REPORT_TYPES = [
    ("refund_report", RefundReport),
    ("itemized_purchase_report", ItemizedPurchaseReport),
    ("university_revenue_share", UniversityRevenueShareReport),
    ("certificate_status", CertificateStatusReport),
]


def initialize_report(report_type, start_date, end_date, start_letter=None, end_letter=None):
    """
    Creates the appropriate type of Report object based on the string report_type.
    """
    for item in REPORT_TYPES:
        if report_type in item:
            return item[1](start_date, end_date, start_letter, end_letter)
    raise ReportTypeDoesNotExistException


@require_POST
def add_course_to_cart(request, course_id):
    """
    Adds course specified by course_id to the cart.  The model function add_to_order does all the
    heavy lifting (logging, error checking, etc)
    """

    assert isinstance(course_id, basestring)
    if not request.user.is_authenticated():
        log.info("Anon user trying to add course {} to cart".format(course_id))
        return HttpResponseForbidden(_('You must be logged-in to add to a shopping cart'))
    cart = Order.get_cart_for_user(request.user)
    course_key = SlashSeparatedCourseKey.from_deprecated_string(course_id)
    # All logging from here handled by the model
    try:
        paid_course_item = PaidCourseRegistration.add_to_order(cart, course_key)
    except CourseDoesNotExistException:
        return HttpResponseNotFound(_('The course you requested does not exist.'))
    except ItemAlreadyInCartException:
        return HttpResponseBadRequest(_('The course {course_id} is already in your cart.'.format(course_id=course_id)))
    except AlreadyEnrolledInCourseException:
        return HttpResponseBadRequest(
            _('You are already registered in course {course_id}.'.format(course_id=course_id)))
    else:
        # in case a coupon redemption code has been applied, new items should also get a discount if applicable.
        order = paid_course_item.order
        order_items = order.orderitem_set.all().select_subclasses()
        redeemed_coupons = CouponRedemption.objects.filter(order=order)
        for redeemed_coupon in redeemed_coupons:
            if Coupon.objects.filter(code=redeemed_coupon.coupon.code, course_id=course_key, is_active=True).exists():
                coupon = Coupon.objects.get(code=redeemed_coupon.coupon.code, course_id=course_key, is_active=True)
                CouponRedemption.add_coupon_redemption(coupon, order, order_items)
                break  # Since only one code can be applied to the cart, we'll just take the first one and then break.

    return HttpResponse(_("Course added to cart."))


@login_required
@enforce_shopping_cart_enabled
def update_user_cart(request):
    """
    when user change the number-of-students from the UI then
    this method Update the corresponding qty field in OrderItem model and update the order_type in order model.
    """
    try:
        qty = int(request.POST.get('qty', -1))
    except ValueError:
        log.exception('Quantity must be an integer.')
        return HttpResponseBadRequest('Quantity must be an integer.')

    if not 1 <= qty <= 1000:
        log.warning('Quantity must be between 1 and 1000.')
        return HttpResponseBadRequest('Quantity must be between 1 and 1000.')

    item_id = request.POST.get('ItemId', None)
    if item_id:
        try:
            item = OrderItem.objects.get(id=item_id, status='cart')
        except OrderItem.DoesNotExist:
            log.exception('Cart OrderItem id={item_id} DoesNotExist'.format(item_id=item_id))
            return HttpResponseNotFound('Order item does not exist.')

        item.qty = qty
        item.save()
        old_to_new_id_map = item.order.update_order_type()
        total_cost = item.order.total_cost

        return JsonResponse({"total_cost": total_cost, "oldToNewIdMap": old_to_new_id_map}, 200)

    return HttpResponseBadRequest('Order item not found in request.')


@login_required
@enforce_shopping_cart_enabled
def show_cart(request):
    """
    This view shows cart items.
    """
    cart = Order.get_cart_for_user(request.user)
    is_any_course_expired, expired_cart_items, expired_cart_item_names, valid_cart_item_tuples = \
        verify_for_closed_enrollment(request.user, cart)
    site_name = microsite.get_value('SITE_NAME', settings.SITE_NAME)

    if is_any_course_expired:
        for expired_item in expired_cart_items:
            Order.remove_cart_item_from_order(expired_item)
        cart.update_order_type()

    appended_expired_course_names = ", ".join(expired_cart_item_names)

    callback_url = request.build_absolute_uri(
        reverse("shoppingcart.views.postpay_callback")
    )
    form_html = render_purchase_form_html(cart, callback_url=callback_url)
    context = {
        'order': cart,
        'shoppingcart_items': valid_cart_item_tuples,
        'amount': cart.total_cost,
        'is_course_enrollment_closed': is_any_course_expired,
        'appended_expired_course_names': appended_expired_course_names,
        'site_name': site_name,
        'form_html': form_html,
        'currency_symbol': settings.PAID_COURSE_REGISTRATION_CURRENCY[1],
        'currency': settings.PAID_COURSE_REGISTRATION_CURRENCY[0],
    }
    return render_to_response("shoppingcart/shopping_cart.html", context)


@login_required
@enforce_shopping_cart_enabled
def clear_cart(request):
    cart = Order.get_cart_for_user(request.user)
    cart.clear()
    coupon_redemption = CouponRedemption.objects.filter(user=request.user, order=cart.id)
    if coupon_redemption:
        coupon_redemption.delete()
        log.info('Coupon redemption entry removed for user {user} for order {order_id}'.format(user=request.user,
                                                                                               order_id=cart.id))

    return HttpResponse('Cleared')


@login_required
@enforce_shopping_cart_enabled
def remove_item(request):
    """
    This will remove an item from the user cart and also delete the corresponding coupon codes redemption.
    """
    item_id = request.REQUEST.get('id', '-1')

    items = OrderItem.objects.filter(id=item_id, status='cart').select_subclasses()

    if not len(items):
        log.exception('Cannot remove cart OrderItem id={item_id}. DoesNotExist or item is already purchased'.format(
            item_id=item_id))
    else:
        item = items[0]
        if item.user == request.user:
            order_item_course_id = getattr(item, 'course_id')
            item.delete()
            log.info('order item {item_id} removed for user {user}'.format(item_id=item_id, user=request.user))
            remove_code_redemption(order_item_course_id, item_id, item, request.user)
            item.order.update_order_type()

    return HttpResponse('OK')


def remove_code_redemption(order_item_course_id, item_id, item, user):
    """
    If an item removed from shopping cart then we will remove
    the corresponding redemption info of coupon code
    """
    try:
        # Try to remove redemption information of coupon code, If exist.
        coupon_redemption = CouponRedemption.objects.get(
            user=user,
            coupon__course_id=order_item_course_id if order_item_course_id else CourseKeyField.Empty,
            order=item.order_id
        )
        coupon_redemption.delete()
        log.info('Coupon "{code}" redemption entry removed for user "{user}" for order item "{item_id}"'
                 .format(code=coupon_redemption.coupon.code, user=user, item_id=item_id))
    except CouponRedemption.DoesNotExist:
        log.debug('Code redemption does not exist for order item id={item_id}.'.format(item_id=item_id))


@login_required
@enforce_shopping_cart_enabled
def reset_code_redemption(request):
    """
    This method reset the code redemption from user cart items.
    """
    cart = Order.get_cart_for_user(request.user)
    cart.reset_cart_items_prices()
    CouponRedemption.delete_coupon_redemption(request.user, cart)
    return HttpResponse('reset')


@login_required
@enforce_shopping_cart_enabled
def use_code(request):
    """
    Valid Code can be either Coupon or Registration code.
    For a valid Coupon Code, this applies the coupon code and generates a discount against all applicable items.
    For a valid Registration code, it deletes the item from the shopping cart and redirects to the
    Registration Code Redemption page.
    """
    code = request.POST["code"]
    coupons = Coupon.objects.filter(
        Q(code=code),
        Q(is_active=True),
        Q(expiration_date__gt=datetime.datetime.now(pytz.UTC)) |
        Q(expiration_date__isnull=True)
    )
    if not coupons:
        # If no coupons then we check that code against course registration code
        try:
            course_reg = CourseRegistrationCode.objects.get(code=code)
        except CourseRegistrationCode.DoesNotExist:
            return HttpResponseNotFound(_("Discount does not exist against code '{code}'.".format(code=code)))

        return use_registration_code(course_reg, request.user)

    return use_coupon_code(coupons, request.user)


def get_reg_code_validity(registration_code, request, limiter):
    """
    This function checks if the registration code is valid, and then checks if it was already redeemed.
    """
    reg_code_already_redeemed = False
    course_registration = None
    try:
        course_registration = CourseRegistrationCode.objects.get(code=registration_code)
    except CourseRegistrationCode.DoesNotExist:
        reg_code_is_valid = False
    else:
        reg_code_is_valid = True
        try:
            RegistrationCodeRedemption.objects.get(registration_code__code=registration_code)
        except RegistrationCodeRedemption.DoesNotExist:
            reg_code_already_redeemed = False
        else:
            reg_code_already_redeemed = True

    if not reg_code_is_valid:
        #tick the rate limiter counter
        AUDIT_LOG.info("Redemption of a non existing RegistrationCode {code}".format(code=registration_code))
        limiter.tick_bad_request_counter(request)
        raise Http404()

    return reg_code_is_valid, reg_code_already_redeemed, course_registration


@require_http_methods(["GET", "POST"])
@login_required
def register_code_redemption(request, registration_code):
    """
    This view allows the student to redeem the registration code
    and enroll in the course.
    """

    # Add some rate limiting here by re-using the RateLimitMixin as a helper class
    site_name = microsite.get_value('SITE_NAME', settings.SITE_NAME)
    limiter = BadRequestRateLimiter()
    if limiter.is_rate_limit_exceeded(request):
        AUDIT_LOG.warning("Rate limit exceeded in registration code redemption.")
        return HttpResponseForbidden()

    template_to_render = 'shoppingcart/registration_code_redemption.html'
    if request.method == "GET":
        reg_code_is_valid, reg_code_already_redeemed, course_registration = get_reg_code_validity(registration_code,
                                                                                                  request, limiter)
        course = get_course_by_id(getattr(course_registration, 'course_id'), depth=0)
        context = {
            'reg_code_already_redeemed': reg_code_already_redeemed,
            'reg_code_is_valid': reg_code_is_valid,
            'reg_code': registration_code,
            'site_name': site_name,
            'course': course,
            'registered_for_course': registered_for_course(course, request.user)
        }
        return render_to_response(template_to_render, context)
    elif request.method == "POST":
        reg_code_is_valid, reg_code_already_redeemed, course_registration = get_reg_code_validity(registration_code,
                                                                                                  request, limiter)
        course = get_course_by_id(getattr(course_registration, 'course_id'), depth=0)
        context = {
            'reg_code': registration_code,
            'site_name': site_name,
            'course': course,
            'reg_code_is_valid': reg_code_is_valid,
            'reg_code_already_redeemed': reg_code_already_redeemed,
        }
        if reg_code_is_valid and not reg_code_already_redeemed:
            # remove the course from the cart if it was added there.
            cart = Order.get_cart_for_user(request.user)
            try:
                cart_items = cart.find_item_by_course_id(course_registration.course_id)

            except ItemNotFoundInCartException:
                pass
            else:
                for cart_item in cart_items:
                    if isinstance(cart_item, PaidCourseRegistration) or isinstance(cart_item, CourseRegCodeItem):
                        cart_item.delete()

            #now redeem the reg code.
            redemption = RegistrationCodeRedemption.create_invoice_generated_registration_redemption(course_registration, request.user)
            try:
                kwargs = {}
                if course_registration.mode_slug is not None:
                    if CourseMode.mode_for_course(course.id, course_registration.mode_slug):
                        kwargs['mode'] = course_registration.mode_slug
                    else:
                        raise RedemptionCodeError()
                redemption.course_enrollment = CourseEnrollment.enroll(request.user, course.id, **kwargs)
                redemption.save()
                context['redemption_success'] = True
            except RedemptionCodeError:
                context['redeem_code_error'] = True
                context['redemption_success'] = False
            except EnrollmentClosedError:
                context['enrollment_closed'] = True
                context['redemption_success'] = False
            except CourseFullError:
                context['course_full'] = True
                context['redemption_success'] = False
            except AlreadyEnrolledError:
                context['registered_for_course'] = True
                context['redemption_success'] = False
        else:
            context['redemption_success'] = False
        return render_to_response(template_to_render, context)


def use_registration_code(course_reg, user):
    """
    This method utilize course registration code.
    If the registration code is already redeemed, it returns an error.
    Else, it identifies and removes the applicable OrderItem from the Order
    and redirects the user to the Registration code redemption page.
    """
    if RegistrationCodeRedemption.is_registration_code_redeemed(course_reg):
        log.warning("Registration code '{registration_code}' already used".format(registration_code=course_reg.code))
        return HttpResponseBadRequest(_(
            "Oops! The code '{registration_code}' you entered is either invalid or expired".format(
                registration_code=course_reg.code)))
    try:
        cart = Order.get_cart_for_user(user)
        cart_items = cart.find_item_by_course_id(course_reg.course_id)
    except ItemNotFoundInCartException:
        log.warning("Course item does not exist against registration code '{registration_code}'".format(
            registration_code=course_reg.code))
        return HttpResponseNotFound(_(
            "Code '{registration_code}' is not valid for any course in the shopping cart.".format(
                registration_code=course_reg.code)))
    else:
        applicable_cart_items = [
            cart_item for cart_item in cart_items
            if (
                (isinstance(cart_item, PaidCourseRegistration) or isinstance(cart_item, CourseRegCodeItem))and cart_item.qty == 1
            )
        ]
        if not applicable_cart_items:
            return HttpResponseNotFound(
                _("Cart item quantity should not be greater than 1 when applying activation code"))

    redemption_url = reverse('register_code_redemption', kwargs={'registration_code': course_reg.code})
    return HttpResponse(
        json.dumps({'response': 'success', 'coupon_code_applied': False, 'redemption_url': redemption_url}),
        content_type="application/json"
    )


def use_coupon_code(coupons, user):
    """
    This method utilize course coupon code
    """
    cart = Order.get_cart_for_user(user)
    cart_items = cart.orderitem_set.all().select_subclasses()
    is_redemption_applied = False
    for coupon in coupons:
        try:
            if CouponRedemption.add_coupon_redemption(coupon, cart, cart_items):
                is_redemption_applied = True
        except MultipleCouponsNotAllowedException:
            return HttpResponseBadRequest(_("Only one coupon redemption is allowed against an order"))

    if not is_redemption_applied:
        log.warning("Discount does not exist against code '{code}'.".format(code=coupons[0].code))
        return HttpResponseNotFound(_("Discount does not exist against code '{code}'.".format(code=coupons[0].code)))

    return HttpResponse(
        json.dumps({'response': 'success', 'coupon_code_applied': True}),
        content_type="application/json"
    )


@require_config(DonationConfiguration)
@require_POST
@login_required
def donate(request):
    """Add a single donation item to the cart and proceed to payment.

    Warning: this call will clear all the items in the user's cart
    before adding the new item!

    Arguments:
        request (Request): The Django request object.  This should contain
            a JSON-serialized dictionary with "amount" (string, required),
            and "course_id" (slash-separated course ID string, optional).

    Returns:
        HttpResponse: 200 on success with JSON-encoded dictionary that has keys
            "payment_url" (string) and "payment_params" (dictionary).  The client
            should POST the payment params to the payment URL.
        HttpResponse: 400 invalid amount or course ID.
        HttpResponse: 404 donations are disabled.
        HttpResponse: 405 invalid request method.

    Example usage:

        POST /shoppingcart/donation/
        with params {'amount': '12.34', course_id': 'edX/DemoX/Demo_Course'}
        will respond with the signed purchase params
        that the client can send to the payment processor.

    """
    amount = request.POST.get('amount')
    course_id = request.POST.get('course_id')

    # Check that required parameters are present and valid
    if amount is None:
        msg = u"Request is missing required param 'amount'"
        log.error(msg)
        return HttpResponseBadRequest(msg)
    try:
        amount = (
            decimal.Decimal(amount)
        ).quantize(
            decimal.Decimal('.01'),
            rounding=decimal.ROUND_DOWN
        )
    except decimal.InvalidOperation:
        return HttpResponseBadRequest("Could not parse 'amount' as a decimal")

    # Any amount is okay as long as it's greater than 0
    # Since we've already quantized the amount to 0.01
    # and rounded down, we can check if it's less than 0.01
    if amount < decimal.Decimal('0.01'):
        return HttpResponseBadRequest("Amount must be greater than 0")

    if course_id is not None:
        try:
            course_id = CourseLocator.from_string(course_id)
        except InvalidKeyError:
            msg = u"Request included an invalid course key: {course_key}".format(course_key=course_id)
            log.error(msg)
            return HttpResponseBadRequest(msg)

    # Add the donation to the user's cart
    cart = Order.get_cart_for_user(request.user)
    cart.clear()

    try:
        # Course ID may be None if this is a donation to the entire organization
        Donation.add_to_order(cart, amount, course_id=course_id)
    except InvalidCartItem as ex:
        log.exception((
            u"Could not create donation item for "
            u"amount '{amount}' and course ID '{course_id}'"
        ).format(amount=amount, course_id=course_id))
        return HttpResponseBadRequest(unicode(ex))

    # Start the purchase.
    # This will "lock" the purchase so the user can't change
    # the amount after we send the information to the payment processor.
    # If the user tries to make another donation, it will be added
    # to a new cart.
    cart.start_purchase()

    # Construct the response params (JSON-encoded)
    callback_url = request.build_absolute_uri(
        reverse("shoppingcart.views.postpay_callback")
    )

    # Add extra to make it easier to track transactions
    extra_data = [
        unicode(course_id) if course_id else "",
        "donation_course" if course_id else "donation_general"
    ]

    response_params = json.dumps({
        # The HTTP end-point for the payment processor.
        "payment_url": get_purchase_endpoint(),

        # Parameters the client should send to the payment processor
        "payment_params": get_signed_purchase_params(
            cart,
            callback_url=callback_url,
            extra_data=extra_data
        ),
    })

    return HttpResponse(response_params, content_type="text/json")


@csrf_exempt
#@require_POST
@require_http_methods(["GET", "POST"])
def postpay_callback(request):
    """
    Receives the POST-back from processor.
    Mainly this calls the processor-specific code to check if the payment was accepted, and to record the order
    if it was, and to generate an error page.
    If successful this function should have the side effect of changing the "cart" into a full "order" in the DB.
    The cart can then render a success page which links to receipt pages.
    If unsuccessful the order will be left untouched and HTML messages giving more detailed error info will be
    returned.
    """
    #params = request.POST.dict()
    params = (request.POST.dict() if request.method == "POST" else request.GET.dict())
    log.info("REQUEST %s: %s", request.method, str(request))
    log.info("SESSION %s: %s", request.method, request.session.items())
    AUDIT_LOG.info("USER ID: %s", str(request.user.id))
    params['user'] = get_user(request)
    params['method'] = request.method
    result = process_postpay_callback(params)
    if result and result['success']:
        return HttpResponseRedirect(reverse('shoppingcart.views.show_receipt', args=[result['order'].id]))
    else:
        return render_to_response('shoppingcart/error.html', {'order': result['order'],
                                                              'error_html': result['error_html']})


@require_http_methods(["GET", "POST"])
@login_required
@enforce_shopping_cart_enabled
def billing_details(request):
    """
    This is the view for capturing additional billing details
    in case of the business purchase workflow.
    """

    cart = Order.get_cart_for_user(request.user)
    cart_items = cart.orderitem_set.all().select_subclasses()
    if getattr(cart, 'order_type') != OrderTypes.BUSINESS:
        raise Http404('Page not found!')

    if request.method == "GET":
        callback_url = request.build_absolute_uri(
            reverse("shoppingcart.views.postpay_callback")
        )
        form_html = render_purchase_form_html(cart, callback_url=callback_url)
        total_cost = cart.total_cost
        context = {
            'shoppingcart_items': cart_items,
            'amount': total_cost,
            'form_html': form_html,
            'currency_symbol': settings.PAID_COURSE_REGISTRATION_CURRENCY[1],
            'currency': settings.PAID_COURSE_REGISTRATION_CURRENCY[0],
            'site_name': microsite.get_value('SITE_NAME', settings.SITE_NAME),
        }
        return render_to_response("shoppingcart/billing_details.html", context)
    elif request.method == "POST":
        company_name = request.POST.get("company_name", "")
        company_contact_name = request.POST.get("company_contact_name", "")
        company_contact_email = request.POST.get("company_contact_email", "")
        recipient_name = request.POST.get("recipient_name", "")
        recipient_email = request.POST.get("recipient_email", "")
        customer_reference_number = request.POST.get("customer_reference_number", "")

        cart.add_billing_details(company_name, company_contact_name, company_contact_email, recipient_name,
                                 recipient_email, customer_reference_number)

        is_any_course_expired, __, __, __ = verify_for_closed_enrollment(request.user)

        return JsonResponse({
            'response': _('success'),
            'is_course_enrollment_closed': is_any_course_expired
        })  # status code 200: OK by default


def verify_for_closed_enrollment(user, cart=None):
    """
    A multi-output helper function.
    inputs:
        user: a user object
        cart: If a cart is provided it uses the same object, otherwise fetches the user's cart.
    Returns:
        is_any_course_expired: True if any of the items in the cart has it's enrollment period closed. False otherwise.
        expired_cart_items: List of courses with enrollment period closed.
        expired_cart_item_names: List of names of the courses with enrollment period closed.
        valid_cart_item_tuples: List of courses which are still open for enrollment.
    """
    if cart is None:
        cart = Order.get_cart_for_user(user)
    expired_cart_items = []
    expired_cart_item_names = []
    valid_cart_item_tuples = []
    cart_items = cart.orderitem_set.all().select_subclasses()
    is_any_course_expired = False
    for cart_item in cart_items:
        course_key = getattr(cart_item, 'course_id', None)
        if course_key is not None:
            course = get_course_by_id(course_key, depth=0)
            if CourseEnrollment.is_enrollment_closed(user, course):
                is_any_course_expired = True
                expired_cart_items.append(cart_item)
                expired_cart_item_names.append(course.display_name)
            else:
                valid_cart_item_tuples.append((cart_item, course))

    return is_any_course_expired, expired_cart_items, expired_cart_item_names, valid_cart_item_tuples


@require_http_methods(["GET"])
@login_required
@enforce_shopping_cart_enabled
def verify_cart(request):
    """
    Called when the user clicks the button to transfer control to CyberSource.
    Returns a JSON response with is_course_enrollment_closed set to True if any of the courses has its
    enrollment period closed. If all courses are still valid, is_course_enrollment_closed set to False.
    """
    is_any_course_expired, __, __, __ = verify_for_closed_enrollment(request.user)
    return JsonResponse(
        {
            'is_course_enrollment_closed': is_any_course_expired
        }
    )  # status code 200: OK by default


@login_required
def show_receipt(request, ordernum):
    """
    Displays a receipt for a particular order.
    404 if order is not yet purchased or request.user != order.user
    """
    try:
        order = Order.objects.get(id=ordernum)
    except Order.DoesNotExist:
        raise Http404('Order not found!')

    if order.user != request.user or order.status not in ['purchased', 'refunded']:
        raise Http404('Order not found!')

    if 'application/json' in request.META.get('HTTP_ACCEPT', ""):
        return _show_receipt_json(order)
    else:
        return _show_receipt_html(request, order)


def _show_receipt_json(order):
    """Render the receipt page as JSON.

    The included information is deliberately minimal:
    as much as possible, the included information should
    be common to *all* order items, so the client doesn't
    need to handle different item types differently.

    Arguments:
        request (HttpRequest): The request for the receipt.
        order (Order): The order model to display.

    Returns:
        HttpResponse

    """
    order_info = {
        'orderNum': order.id,
        'currency': order.currency,
        'status': order.status,
        'purchase_datetime': get_default_time_display(order.purchase_time) if order.purchase_time else None,
        'billed_to': {
            'first_name': order.bill_to_first,
            'last_name': order.bill_to_last,
            'street1': order.bill_to_street1,
            'street2': order.bill_to_street2,
            'city': order.bill_to_city,
            'state': order.bill_to_state,
            'postal_code': order.bill_to_postalcode,
            'country': order.bill_to_country,
        },
        'total_cost': order.total_cost,
        'items': [
            {
                'quantity': item.qty,
                'unit_cost': item.unit_cost,
                'line_cost': item.line_cost,
                'line_desc': item.line_desc
            }
            for item in OrderItem.objects.filter(order=order).select_subclasses()
        ]
    }
    return JsonResponse(order_info)


def _show_receipt_html(request, order):
    """Render the receipt page as HTML.

    Arguments:
        request (HttpRequest): The request for the receipt.
        order (Order): The order model to display.

    Returns:
        HttpResponse

    """
    order_items = OrderItem.objects.filter(order=order).select_subclasses()
    shoppingcart_items = []
    course_names_list = []
    for order_item in order_items:
        course_key = getattr(order_item, 'course_id')
        if course_key:
            course = get_course_by_id(course_key, depth=0)
            shoppingcart_items.append((order_item, course))
            course_names_list.append(course.display_name)

    appended_course_names = ", ".join(course_names_list)
    any_refunds = any(i.status == "refunded" for i in order_items)
    receipt_template = 'shoppingcart/receipt.html'
    __, instructions = order.generate_receipt_instructions()
    order_type = getattr(order, 'order_type')

    # Only orders where order_items.count() == 1 might be attempting to upgrade
    attempting_upgrade = request.session.get('attempting_upgrade', False)
    if attempting_upgrade:
        course_enrollment = CourseEnrollment.get_or_create_enrollment(request.user, order_items[0].course_id)
        course_enrollment.emit_event(EVENT_NAME_USER_UPGRADED)
        request.session['attempting_upgrade'] = False

    recipient_list = []
    total_registration_codes = None
    reg_code_info_list = []
    recipient_list.append(getattr(order.user, 'email'))
    if order_type == OrderTypes.BUSINESS:
        if order.company_contact_email:
            recipient_list.append(order.company_contact_email)
        if order.recipient_email:
            recipient_list.append(order.recipient_email)

        for __, course in shoppingcart_items:
            course_registration_codes = CourseRegistrationCode.objects.filter(order=order, course_id=course.id)
            total_registration_codes = course_registration_codes.count()
            for course_registration_code in course_registration_codes:
                reg_code_info_list.append({
                    'course_name': course.display_name,
                    'redemption_url': reverse('register_code_redemption', args=[course_registration_code.code]),
                    'code': course_registration_code.code,
                    'is_redeemed': RegistrationCodeRedemption.objects.filter(
                        registration_code=course_registration_code).exists(),
                })

    appended_recipient_emails = ", ".join(recipient_list)

    context = {
        'order': order,
        'shoppingcart_items': shoppingcart_items,
        'any_refunds': any_refunds,
        'instructions': instructions,
        'site_name': microsite.get_value('SITE_NAME', settings.SITE_NAME),
        'order_type': order_type,
        'appended_course_names': appended_course_names,
        'appended_recipient_emails': appended_recipient_emails,
        'currency_symbol': settings.PAID_COURSE_REGISTRATION_CURRENCY[1],
        'currency': settings.PAID_COURSE_REGISTRATION_CURRENCY[0],
        'total_registration_codes': total_registration_codes,
        'reg_code_info_list': reg_code_info_list,
        'order_purchase_date': order.purchase_time.strftime("%B %d, %Y") if order.purchase_time else None,
    }
    # we want to have the ability to override the default receipt page when
    # there is only one item in the order
    if order_items.count() == 1:
        receipt_template = order_items[0].single_item_receipt_template
        context.update(order_items[0].single_item_receipt_context)

        # TODO (ECOM-188): Once the A/B test of separate verified / payment flow
        # completes, implement this in a more general way.  For now,
        # we simply redirect to the new receipt page (in verify_student).
        if settings.FEATURES.get('SEPARATE_VERIFICATION_FROM_PAYMENT') and request.session.get('separate-verified', False):
            if receipt_template == 'shoppingcart/verified_cert_receipt.html':
                url = reverse(
                    'verify_student_payment_confirmation',
                    kwargs={'course_id': unicode(order_items[0].course_id)}
                )

                # Add a query string param for the order ID
                # This allows the view to query for the receipt information later.
                url += '?payment-order-num={order_num}'.format(
                    order_num=order_items[0].order.id
                )
                return HttpResponseRedirect(url)

    return render_to_response(receipt_template, context)


def _can_download_report(user):
    """
    Tests if the user can download the payments report, based on membership in a group whose name is determined
     in settings.  If the group does not exist, denies all access
    """
    try:
        access_group = Group.objects.get(name=settings.PAYMENT_REPORT_GENERATOR_GROUP)
    except Group.DoesNotExist:
        return False
    return access_group in user.groups.all()


def _get_date_from_str(date_input):
    """
    Gets date from the date input string.  Lets the ValueError raised by invalid strings be processed by the caller
    """
    return datetime.datetime.strptime(date_input.strip(), "%Y-%m-%d").replace(tzinfo=pytz.UTC)


def _render_report_form(start_str, end_str, start_letter, end_letter, report_type, total_count_error=False, date_fmt_error=False):
    """
    Helper function that renders the purchase form.  Reduces repetition
    """
    context = {
        'total_count_error': total_count_error,
        'date_fmt_error': date_fmt_error,
        'start_date': start_str,
        'end_date': end_str,
        'start_letter': start_letter,
        'end_letter': end_letter,
        'requested_report': report_type,
    }
    return render_to_response('shoppingcart/download_report.html', context)


@login_required
def csv_report(request):
    """
    Downloads csv reporting of orderitems
    """
    if not _can_download_report(request.user):
        return HttpResponseForbidden(_('You do not have permission to view this page.'))

    if request.method == 'POST':
        start_date = request.POST.get('start_date', '')
        end_date = request.POST.get('end_date', '')
        start_letter = request.POST.get('start_letter', '')
        end_letter = request.POST.get('end_letter', '')
        report_type = request.POST.get('requested_report', '')
        try:
            start_date = _get_date_from_str(start_date) + datetime.timedelta(days=0)
            end_date = _get_date_from_str(end_date) + datetime.timedelta(days=1)
        except ValueError:
            # Error case: there was a badly formatted user-input date string
            return _render_report_form(start_date, end_date, start_letter, end_letter, report_type, date_fmt_error=True)

        report = initialize_report(report_type, start_date, end_date, start_letter, end_letter)
        items = report.rows()

        response = HttpResponse(mimetype='text/csv')
        filename = "purchases_report_{}.csv".format(datetime.datetime.now(pytz.UTC).strftime("%Y-%m-%d-%H-%M-%S"))
        response['Content-Disposition'] = 'attachment; filename="{}"'.format(filename)
        report.write_csv(response)
        return response

    elif request.method == 'GET':
        end_date = datetime.datetime.now(pytz.UTC)
        start_date = end_date - datetime.timedelta(days=30)
        start_letter = ""
        end_letter = ""
        return _render_report_form(start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d"), start_letter, end_letter, report_type="")

    else:
        return HttpResponseBadRequest("HTTP Method Not Supported")
