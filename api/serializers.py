from decimal import Decimal
from rest_framework import serializers
from django.db.models import Q
from django.contrib.auth.models import User as DjangoUser
from .models import Client, UserProfile, Property, PropertyImage, PropertySlot, Booking, Payment, Enquiry


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class ClientSerializer(serializers.ModelSerializer):
    user_count = serializers.SerializerMethodField()

    class Meta:
        model = Client
        fields = ['id', 'client_id', 'name', 'email', 'phone', 'is_active', 'created_at', 'user_count']
        read_only_fields = ['created_at']

    def get_user_count(self, obj):
        return obj.users.count()


# ---------------------------------------------------------------------------
# UserProfile / User management
# ---------------------------------------------------------------------------

class UserProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserProfile
        fields = ['role', 'client']


class UserSerializer(serializers.ModelSerializer):
    """
    Full user representation including profile. Used by super_admin user
    management endpoints.
    """
    role = serializers.CharField(source='profile.role', read_only=True)
    client_id = serializers.CharField(source='profile.client.client_id', read_only=True, allow_null=True)
    client_name = serializers.CharField(source='profile.client.name', read_only=True, allow_null=True)

    class Meta:
        model = DjangoUser
        fields = ['id', 'username', 'email', 'is_active', 'role', 'client_id', 'client_name', 'date_joined']
        read_only_fields = ['date_joined']


class CreateUserSerializer(serializers.Serializer):
    """
    Used by POST /api/users/ (super_admin only). Creates a Django user +
    UserProfile in one call. The password is write-only and hashed
    automatically.
    """
    username = serializers.CharField(max_length=150)
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, min_length=8)
    role = serializers.ChoiceField(choices=UserProfile.ROLES, default='admin')
    # client_id is required for admin/staff; super_admin can be left blank
    client_id = serializers.CharField(max_length=50, required=False, allow_blank=True)

    def validate_username(self, value):
        if DjangoUser.objects.filter(username=value).exists():
            raise serializers.ValidationError("A user with that username already exists.")
        return value

    def validate_email(self, value):
        if DjangoUser.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("A user with that email already exists.")
        return value

    def validate(self, data):
        role = data.get('role', 'admin')
        client_id = data.get('client_id', '').strip().upper()

        if role != 'super_admin':
            if not client_id:
                raise serializers.ValidationError(
                    {"client_id": "client_id is required for admin / staff users."}
                )
            try:
                data['_client'] = Client.objects.get(client_id=client_id)
            except Client.DoesNotExist:
                raise serializers.ValidationError(
                    {"client_id": f"Client '{client_id}' does not exist."}
                )
        else:
            data['_client'] = None
        return data

    def create(self, validated_data):
        client = validated_data.pop('_client')
        validated_data.pop('client_id', None)

        user = DjangoUser.objects.create_user(
            username=validated_data['username'],
            email=validated_data['email'],
            password=validated_data['password'],
        )
        UserProfile.objects.create(
            user=user,
            role=validated_data.get('role', 'admin'),
            client=client,
        )
        return user


# ---------------------------------------------------------------------------
# Property
# ---------------------------------------------------------------------------

class PropertyImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = PropertyImage
        fields = ['id', 'image', 'is_primary', 'uploaded_at']


class PropertySlotSerializer(serializers.ModelSerializer):
    class Meta:
        model = PropertySlot
        fields = [
            'id', 'slot_id', 'property', 'slot_type',
            'price', 'min_duration_hours', 'max_duration_hours', 'status',
        ]
        # slot_id is auto-generated on first save — never writable via API
        read_only_fields = ['slot_id']
        extra_kwargs = {'property': {'required': False}}


class PropertySerializer(serializers.ModelSerializer):
    images = PropertyImageSerializer(many=True, read_only=True)
    slots = PropertySlotSerializer(many=True, required=False)
    primary_image = serializers.SerializerMethodField()
    total_bookings = serializers.SerializerMethodField()

    class Meta:
        model = Property
        fields = [
            'id', 'property_id', 'name', 'property_type', 'description', 'address',
            'location', 'google_map_link', 'capacity', 'security_deposit', 'status',
            'images', 'slots', 'primary_image', 'total_bookings',
            'created_at', 'updated_at',
        ]
        # client is injected by the view from request.user.profile.client
        # property_id is auto-generated on first save — never writable via API
        read_only_fields = ['property_id', 'created_at', 'updated_at']

    def get_primary_image(self, obj):
        img = obj.images.filter(is_primary=True).first() or obj.images.first()
        if img:
            request = self.context.get('request')
            return request.build_absolute_uri(img.image.url) if request else img.image.url
        return None

    def get_total_bookings(self, obj):
        return obj.bookings.exclude(status='cancelled').count()

    def create(self, validated_data):
        slots_data = validated_data.pop('slots', [])
        # 'client' must have been injected into validated_data by the view
        property_obj = Property.objects.create(**validated_data)
        for slot_data in slots_data:
            PropertySlot.objects.create(property=property_obj, **slot_data)
        return property_obj

    def update(self, instance, validated_data):
        slots_data = validated_data.pop('slots', None)
        validated_data.pop('client', None)   # never reassign tenant on update
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        if slots_data is not None:
            instance.slots.all().delete()
            for slot_data in slots_data:
                PropertySlot.objects.create(property=instance, **slot_data)
        return instance


# ---------------------------------------------------------------------------
# Payment
# ---------------------------------------------------------------------------

class PaymentSerializer(serializers.ModelSerializer):
    booking_total = serializers.DecimalField(
        source='booking.total_amount', max_digits=10, decimal_places=2, read_only=True)
    booking_paid = serializers.DecimalField(
        source='booking.advance_amount', max_digits=10, decimal_places=2, read_only=True)
    booking_balance = serializers.DecimalField(
        source='booking.balance_amount', max_digits=10, decimal_places=2, read_only=True)
    payment_status = serializers.CharField(
        source='booking.payment_status', read_only=True)

    class Meta:
        model = Payment
        fields = [
            'id', 'booking', 'amount', 'payment_method',
            'payment_date', 'reference', 'notes', 'received_at',
            'booking_total', 'booking_paid', 'booking_balance', 'payment_status',
        ]
        read_only_fields = ['received_at']


# ---------------------------------------------------------------------------
# Booking
# ---------------------------------------------------------------------------

class BookingListSerializer(serializers.ModelSerializer):
    property_name = serializers.CharField(source='property.name', read_only=True)
    slot_type = serializers.CharField(source='property_slot.slot_type', read_only=True)

    class Meta:
        model = Booking
        fields = [
            'id', 'booking_number', 'property', 'property_name',
            'property_slot', 'slot_type',
            'customer_name', 'mobile_number',
            'event_name', 'event_type', 'booking_date',
            'start_time', 'end_time', 'duration_hours',
            'total_amount', 'advance_amount', 'balance_amount',
            'payment_status', 'status', 'created_at',
        ]


class BookingDetailSerializer(serializers.ModelSerializer):
    property_detail = PropertySerializer(source='property', read_only=True)
    property_slot_detail = PropertySlotSerializer(source='property_slot', read_only=True)
    payments = PaymentSerializer(many=True, read_only=True)

    class Meta:
        model = Booking
        fields = [
            'id', 'booking_number',
            'property', 'property_detail',
            'property_slot', 'property_slot_detail',
            'customer_name', 'mobile_number',
            'event_name', 'event_type',
            'booking_date', 'start_time', 'end_time', 'duration_hours',
            'total_amount', 'advance_amount', 'balance_amount',
            'payment_status', 'notes', 'status',
            'payments', 'created_at', 'updated_at',
        ]
        read_only_fields = ['duration_hours', 'total_amount']

    def validate(self, data):
        property_obj = data.get('property') or getattr(self.instance, 'property', None)
        property_slot = data.get('property_slot') or getattr(self.instance, 'property_slot', None)
        booking_date = data.get('booking_date') or getattr(self.instance, 'booking_date', None)
        start = data.get('start_time') or getattr(self.instance, 'start_time', None)
        end = data.get('end_time') or getattr(self.instance, 'end_time', None)

        if not (property_obj and property_slot and booking_date and start and end):
            raise serializers.ValidationError(
                "property, property_slot, booking_date, start_time and end_time are all required."
            )

        if property_slot.property_id != property_obj.id:
            raise serializers.ValidationError("That slot doesn't belong to the selected property.")

        # ── Tenant guard: property must belong to the requesting user's client ─
        request = self.context.get('request')
        if request and hasattr(request.user, 'profile'):
            try:
                user_client = request.user.profile.client
                is_super = request.user.profile.role == 'super_admin'
            except Exception:
                user_client, is_super = None, False
            if not is_super and user_client and property_obj.client_id != user_client.id:
                raise serializers.ValidationError(
                    "You can only book properties that belong to your account."
                )

        if end <= start:
            raise serializers.ValidationError("end_time must be after start_time.")

        if property_slot.slot_type == 'hourly':
            duration = (
                end.hour * 3600 + end.minute * 60
                - start.hour * 3600 - start.minute * 60
            ) / 3600
            if duration < float(property_slot.min_duration_hours):
                raise serializers.ValidationError(
                    f"Minimum booking duration for this slot is {property_slot.min_duration_hours} hours."
                )
            if property_slot.max_duration_hours and duration > float(property_slot.max_duration_hours):
                raise serializers.ValidationError(
                    f"Maximum booking duration for this slot is {property_slot.max_duration_hours} hours."
                )
            data['total_amount'] = Decimal(str(round(float(property_slot.price) * duration, 2)))
        else:
            data['total_amount'] = property_slot.price

        conflict_qs = Booking.objects.filter(
            property=property_obj,
            booking_date=booking_date,
            status__in=['reserved'],
        ).exclude(
            Q(end_time__lte=start) | Q(start_time__gte=end)
        )
        if self.instance:
            conflict_qs = conflict_qs.exclude(pk=self.instance.pk)
        if conflict_qs.exists():
            raise serializers.ValidationError(
                "Booking cannot be created because it overlaps with an existing booking for this property."
            )

        return data


class CalendarBookingSerializer(serializers.ModelSerializer):
    property_name = serializers.CharField(source='property.name', read_only=True)
    slot_type = serializers.CharField(source='property_slot.slot_type', read_only=True)

    class Meta:
        model = Booking
        fields = [
            'id', 'booking_number',
            'property', 'property_name',
            'property_slot', 'slot_type',
            'start_time', 'end_time',
            'customer_name', 'event_name', 'event_type',
            'booking_date', 'status', 'payment_status',
            'total_amount', 'advance_amount', 'balance_amount',
        ]


class DashboardSerializer(serializers.Serializer):
    total_properties = serializers.IntegerField()
    active_properties = serializers.IntegerField()
    total_bookings = serializers.IntegerField()
    today_bookings = serializers.IntegerField()
    this_month_bookings = serializers.IntegerField()
    occupied_slots = serializers.IntegerField()
    total_revenue = serializers.DecimalField(max_digits=12, decimal_places=2)
    pending_payments = serializers.DecimalField(max_digits=12, decimal_places=2)
    occupancy_rate = serializers.FloatField()
    recent_bookings = BookingListSerializer(many=True)
    upcoming_events = BookingListSerializer(many=True)
    status_breakdown = serializers.DictField()
    monthly_revenue = serializers.ListField()


# ---------------------------------------------------------------------------
# Enquiry
# ---------------------------------------------------------------------------

class EnquirySerializer(serializers.ModelSerializer):
    property_name = serializers.CharField(source='property.name', read_only=True)
    slot_type = serializers.CharField(source='property_slot.slot_type', read_only=True, allow_null=True)
    booking_number = serializers.CharField(source='booking.booking_number', read_only=True, allow_null=True)

    class Meta:
        model = Enquiry
        fields = [
            'id', 'enquiry_number',
            'property', 'property_name',
            'property_slot', 'slot_type',
            'customer_name', 'mobile_number', 'email',
            'event_name', 'event_type',
            'enquiry_date', 'start_time', 'end_time',
            'notes', 'status',
            'booking', 'booking_number',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['enquiry_number', 'booking', 'created_at', 'updated_at']
        extra_kwargs = {'property_slot': {'required': False, 'allow_null': True}}