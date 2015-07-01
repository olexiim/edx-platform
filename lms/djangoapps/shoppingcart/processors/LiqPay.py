"""
Implementation of the LiqPay credit card processor using the Callback API 3.0

To enable this implementation, add the following Django settings:

    CC_PROCESSOR_NAME = "LiqPay"
    CC_PROCESSOR = {
        "LiqPay": {
            "PRIVATE_KEY": "<private key>",
            "PURCHASE_ENDPOINT": "<purchase endpoint>"
        }
    }

"""

import base64
import sha
import json
import logging
import re
import uuid
from datetime import datetime
from collections import OrderedDict, defaultdict
from decimal import Decimal, InvalidOperation
from django.conf import settings
from django.utils.translation import ugettext as _
from edxmako.shortcuts import render_to_string
from shoppingcart.models import Order
from shoppingcart.processors.exceptions import *
from shoppingcart.processors.helpers import get_processor_config
from microsite_configuration import microsite

log = logging.getLogger(__name__)


def process_postpay_callback(params):
    """
    Handle a response from the payment processor.

    Concrete implementations should:
        1) Verify the parameters and determine if the payment was successful.
        2) If successful, mark the order as purchased and call `purchased_callbacks` of the cart items.
        3) If unsuccessful, try to figure out why and generate a helpful error message.
        4) Return a dictionary of the form:
            {'success': bool, 'order': Order, 'error_html': str}

    Args:
        params (dict): Dictionary of parameters received from the payment processor.

    Keyword Args:
        Can be used to provide additional information to concrete implementations.

    Returns:
        dict

    """
    if params['method'] == "GET":
        data = { 'success': True }
        cart = Order.objects.filter(user=params['user'], status='purchased').order_by('-id')[:1].get()
        data['order'] = cart
        return data
    else:
        valid_params = verify_signatures(params)
        if valid_params['status'] == 'success' or valid_params['status'] == 'sandbox':
            result = _payment_accepted(
                valid_params['order_id'],
                valid_params['amount'],
                valid_params['currency']
            )

            _record_purchase(valid_params, result['order'])
            return {
                'success': True,
                'order': result['order'],
                'error_html': ''
            }
        # except CCProcessorException as error:
        #     return {
        #        'success': False,
        #        'order': None,  # due to exception we may not have the order
        #        'error_html': "ERROR" #_get_processor_exception_html(error)
        #     }


def processor_hash(value):
    """
    Calculate the base64-encoded, SHA-256 hash used by CyberSource.

    Args:
        value (string): The value to encode.

    Returns:
        string

    """
    private_key = get_processor_config().get('PRIVATE_KEY', None)
    if not private_key:
        raise CCProcessorPrivateKeyAbsenceException()
    return base64.b64encode(sha.new(private_key + value + private_key).digest())


def verify_signatures(params):
    """
    Use the signature we receive in the POST back from CyberSource to verify
    the identity of the sender (CyberSource) and that the contents of the message
    have not been tampered with.

    Args:
        params (dictionary): The POST parameters we received from CyberSource.

    Returns:
        dict: Contains the parameters we will use elsewhere, converted to the
            appropriate types

    Raises:
        CCProcessorSignatureException: The calculated signature does not match
            the signature we received.

        CCProcessorDataException: The parameters we received from CyberSource were not valid
            (missing keys, wrong types)

    """

    # First see if the user cancelled the transaction
    # if so, then not all parameters will be passed back so we can't yet verify signatures
    if params.get('status', '').lower() == u'failure':
        raise CCProcessorFailedTransaction()

    data = params.get('data', '')
    received_signature = params.get('signature', '')

    if not (data or received_signature):
        raise CCProcessorDataException()

    if processor_hash(data) != received_signature:
        raise CCProcessorSignatureException()
                                                                                                                                                         
    # Validate that we have the paramters we expect and can convert them
    # to the appropriate types.
    # Usually validating the signature is sufficient to validate that these
    # fields exist, but since we're relying on CyberSource to tell us
    # which fields they included in the signature, we need to be careful.
    valid_params = {}
    data = json.loads(base64.b64decode(data))
    required_params = [
        ('order_id', int),
        ('currency', str),
        ('status', str),
        ('amount', float),
    ]
    for key, key_type in required_params:
        if key not in data:
            raise CCProcessorDataException(
                _(
                    u"The payment processor did not return a required parameter: {parameter}"
                ).format(parameter=key)
            )
        try:
            valid_params[key] = key_type(data[key])
        except (ValueError, TypeError, InvalidOperation):
            raise CCProcessorDataException(
                _(
                    u"The payment processor returned a badly-typed value {value} for parameter {parameter}."
                ).format(value=data[key], parameter=key)
            )

    return valid_params


def get_purchase_endpoint():
    """
    Return the URL of the payment end-point for CyberSource.

    Returns:
        unicode

    """
    return get_processor_config().get('PURCHASE_ENDPOINT', '')


def _payment_accepted(order_id, auth_amount, currency):
    """
    Check that CyberSource has accepted the payment.

    Args:
        order_num (int): The ID of the order associated with this payment.
        auth_amount (Decimal): The amount the user paid using CyberSource.
        currency (str): The currency code of the payment.
        decision (str): "ACCEPT" if the payment was accepted.

    Returns:
        dictionary of the form:
        {
            'accepted': bool,
            'amnt_charged': int,
            'currency': string,
            'order': Order
        }

    Raises:
        CCProcessorDataException: The order does not exist.
        CCProcessorWrongAmountException: The user did not pay the correct amount.

    """
    try:
        order = Order.objects.get(id=order_id)
    except Order.DoesNotExist:
        raise CCProcessorDataException(_("The payment processor accepted an order whose number is not in our system."))

    if auth_amount == order.total_cost and currency.lower() == order.currency.lower():
        return {
            'amt_charged': auth_amount,
            'currency': currency,
            'order': order
        }
    else:
        ex = CCProcessorWrongAmountException(
            _(
                u"The amount charged by the processor {charged_amount} {charged_amount_currency} is different "
                u"than the total cost of the order {total_cost} {total_cost_currency}."
            ).format(
                charged_amount=auth_amount,
                charged_amount_currency=currency,
                total_cost=order.total_cost,
                total_cost_currency=order.currency
            )
        )
 
        #pylint: disable=attribute-defined-outside-init
        ex.order = order
        raise ex


def _record_purchase(params, order):
    """
    Record the purchase and run purchased_callbacks

    Args:
        params (dict): The parameters we received from CyberSource.
        order (Order): The order associated with this payment.

    Returns:
        None

    """
    # Usually, the credit card number will have the form "xxxxxxxx1234"
    # Parse the string to retrieve the digits.
    # If we can't find any digits, use placeholder values instead.
    ccnum_str = params.get('req_card_number', '')
    mm = re.search("\d", ccnum_str)
    if mm:
        ccnum = ccnum_str[mm.start():]
    else:
        ccnum = "####"

    # Mark the order as purchased and store the billing information
    log.info("ORDER %s", order.status)
    order.purchase(
        first=params.get('req_bill_to_forename', ''),
        last=params.get('req_bill_to_surname', ''),
        street1=params.get('req_bill_to_address_line1', ''),
        street2=params.get('req_bill_to_address_line2', ''),
        city=params.get('req_bill_to_address_city', ''),
        state=params.get('req_bill_to_address_state', ''),
        country=params.get('req_bill_to_address_country', ''),
        postalcode=params.get('req_bill_to_address_postal_code', ''),
        ccnum=ccnum,
        cardtype=params.get('req_card_type', ''),
        processor_reply_dump=json.dumps(params)
    )
    log.info("ORDER2 %s", order.status)

def render_purchase_form_html(cart, callback_url=None, extra_data=None):
    """
    Just stub from CyberSource for now

    """
    return render_to_string('shoppingcart/cybersource_form.html', {
        'action': get_purchase_endpoint(),
        'params': get_signed_purchase_params(
            cart, callback_url=callback_url, extra_data=extra_data
        ),
    })


def get_purchase_endpoint():
    """
    Return the URL of the payment end-point for CyberSource.

    Returns:
        unicode

    """
    return get_processor_config().get('PURCHASE_ENDPOINT', '')


def sign(params):
    """
    Just stub for now
    """
    fields = u",".join(params.keys())
    params['signed_field_names'] = fields

    signed_fields = params.get('signed_field_names', '').split(',')
    values = u",".join([u"{0}={1}".format(i, params.get(i, '')) for i in signed_fields])
    params['signature'] = processor_hash(values)
    params['signed_field_names'] = fields

    return params

def get_purchase_params(cart, callback_url=None, extra_data=None):
    """
    Just stub for now
    """
    total_cost = cart.total_cost
    amount = "{0:0.2f}".format(total_cost)
    params = OrderedDict()

    params['amount'] = amount
    params['currency'] = cart.currency
    params['orderNumber'] = "OrderId: {0:d}".format(cart.id)

    params['access_key'] = get_processor_config().get('ACCESS_KEY', '')
    params['profile_id'] = get_processor_config().get('PROFILE_ID', '')
    params['reference_number'] = cart.id
    params['transaction_type'] = 'sale'

    params['locale'] = 'en'
    params['signed_date_time'] = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
    params['signed_field_names'] = 'access_key,profile_id,amount,currency,transaction_type,reference_number,signed_date_time,locale,transaction_uuid,signed_field_names,unsigned_field_names,orderNumber'
    params['unsigned_field_names'] = ''
    params['transaction_uuid'] = uuid.uuid4().hex
    params['payment_method'] = 'card'
    params['success'] = True

    if callback_url is not None:
        params['override_custom_receipt_page'] = callback_url
        params['override_custom_cancel_page'] = callback_url

    if extra_data is not None:
        # CyberSource allows us to send additional data in "merchant defined data" fields
        for num, item in enumerate(extra_data, start=1):
            key = u"merchant_defined_data{num}".format(num=num)
            params[key] = item

    return params


def get_signed_purchase_params(cart, callback_url=None, extra_data=None):
    """
    Just stub for now
    """
    return get_purchase_params(cart, callback_url=callback_url, extra_data=extra_data)


                                                                

