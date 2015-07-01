from ratelimitbackend import admin
from verify_student.models import SoftwareSecurePhotoVerification

class SoftwareSecurePhotoVerificationAdmin(admin.ModelAdmin):
     readonly_fields = ('face_image_url', 'face_image', 'photo_id_image', 'photo_id_image_url')
     fields = ('status', 'status_changed', 'submitted_at', 'user', 'name', 
               'face_image_url', 'face_image', 'photo_id_image_url', 'photo_id_image', 'receipt_id', 'display',
               'reviewing_user', 'reviewing_service', 'error_msg', 'error_code', 'photo_id_key', 'window')


admin.site.register(SoftwareSecurePhotoVerification, SoftwareSecurePhotoVerificationAdmin)
#admin.site.register(SoftwareSecurePhotoVerification)

