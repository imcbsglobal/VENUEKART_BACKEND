from django.contrib import admin
from .models import Client, UserProfile, Property, PropertyImage, PropertySlot, Booking, Payment


# ---------------------------------------------------------------------------
# Client (tenant)
# ---------------------------------------------------------------------------

class UserProfileInline(admin.TabularInline):
    model = UserProfile
    extra = 0
    readonly_fields = ['user']
    fields = ['user', 'role']


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ['client_id', 'name', 'email', 'phone', 'is_active', 'created_at']
    list_filter = ['is_active']
    search_fields = ['client_id', 'name', 'email']
    inlines = [UserProfileInline]


# ---------------------------------------------------------------------------
# UserProfile — show as a standalone changelist too so you can quickly
# reassign a user's role or switch them to a different client.
# ---------------------------------------------------------------------------

@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ['user', 'client', 'role']
    list_filter = ['role', 'client']
    search_fields = ['user__username', 'user__email', 'client__client_id']
    autocomplete_fields = ['user', 'client']


# ---------------------------------------------------------------------------
# Property
# ---------------------------------------------------------------------------

class PropertyImageInline(admin.TabularInline):
    model = PropertyImage
    extra = 1


class PropertySlotInline(admin.TabularInline):
    model = PropertySlot
    extra = 0
    readonly_fields = ['slot_id']
    fields = ['slot_id', 'slot_type', 'price', 'min_duration_hours', 'max_duration_hours', 'status']


@admin.register(Property)
class PropertyAdmin(admin.ModelAdmin):
    list_display = ['property_id', 'name', 'client', 'property_type', 'location', 'capacity', 'status', 'slot_types_summary']
    list_filter = ['client', 'property_type', 'status']
    search_fields = ['property_id', 'name', 'location', 'address']
    readonly_fields = ['property_id']
    inlines = [PropertyImageInline, PropertySlotInline]
    autocomplete_fields = ['client']

    def slot_types_summary(self, obj):
        types = obj.slots.values_list('slot_type', flat=True).distinct()
        labels = dict(PropertySlot.SLOT_TYPES)
        return ", ".join(labels.get(t, t) for t in types) or "—"
    slot_types_summary.short_description = 'Offers'


# ---------------------------------------------------------------------------
# Booking
# ---------------------------------------------------------------------------

class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 0
    readonly_fields = ['received_at']


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = [
        'booking_number', 'client', 'customer_name', 'property', 'property_slot',
        'booking_date', 'event_name', 'total_amount', 'payment_status', 'status',
    ]
    list_filter = ['client', 'status', 'payment_status', 'event_type', 'property']
    search_fields = ['booking_number', 'customer_name', 'mobile_number', 'event_name']
    readonly_fields = ['booking_number', 'balance_amount', 'payment_status', 'duration_hours']
    inlines = [PaymentInline]
    date_hierarchy = 'booking_date'


# ---------------------------------------------------------------------------
# Payment
# ---------------------------------------------------------------------------

@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ['booking', 'client', 'amount', 'payment_method', 'payment_date', 'received_at']
    list_filter = ['client', 'payment_method']
    search_fields = ['booking__booking_number', 'booking__customer_name', 'reference']