from django.contrib import admin
from .models import Property, PropertyImage, PropertySlot, Booking, Payment


class PropertyImageInline(admin.TabularInline):
    model = PropertyImage
    extra = 1


class PropertySlotInline(admin.TabularInline):
    """
    This is the "tick Full Day / Half Day / Hourly" UI for a property in
    Django admin: add one row per type the property offers, with its
    price. Replaces the old standalone Slot admin page.
    """
    model = PropertySlot
    extra = 0
    fields = ['slot_type', 'price', 'min_duration_hours', 'max_duration_hours', 'status']


@admin.register(Property)
class PropertyAdmin(admin.ModelAdmin):
    list_display = ['name', 'property_type', 'location', 'capacity', 'status', 'slot_types_summary']
    list_filter = ['property_type', 'status']
    search_fields = ['name', 'location', 'address']
    inlines = [PropertyImageInline, PropertySlotInline]

    def slot_types_summary(self, obj):
        types = obj.slots.values_list('slot_type', flat=True).distinct()
        labels = dict(PropertySlot.SLOT_TYPES)
        return ", ".join(labels.get(t, t) for t in types) or "—"
    slot_types_summary.short_description = 'Offers'


class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 0
    readonly_fields = ['received_at']


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = [
        'booking_number', 'customer_name', 'property', 'property_slot',
        'booking_date', 'event_name', 'total_amount', 'payment_status', 'status',
    ]
    list_filter = ['status', 'payment_status', 'event_type', 'property']
    search_fields = ['booking_number', 'customer_name', 'mobile_number', 'event_name']
    readonly_fields = ['booking_number', 'balance_amount', 'payment_status', 'duration_hours']
    inlines = [PaymentInline]
    date_hierarchy = 'booking_date'


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ['booking', 'amount', 'payment_method', 'payment_date', 'received_at']
    list_filter = ['payment_method']
    search_fields = ['booking__booking_number', 'booking__customer_name', 'reference']