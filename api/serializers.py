from rest_framework import serializers
from django.db.models import Q
from .models import Property, PropertyImage, PropertySlot, Booking, Payment


class PropertyImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = PropertyImage
        fields = ['id', 'image', 'is_primary', 'uploaded_at']


class PropertySlotSerializer(serializers.ModelSerializer):
    class Meta:
        model = PropertySlot
        fields = [
            'id', 'property', 'slot_type',
            'price', 'min_duration_hours', 'max_duration_hours', 'status',
        ]
        # 'property' is set by the parent PropertySerializer when slots are
        # created/updated as part of the property form, so it's not required
        # on its own when nested.
        extra_kwargs = {'property': {'required': False}}


class PropertySerializer(serializers.ModelSerializer):
    images = PropertyImageSerializer(many=True, read_only=True)
    # Writable nested slots: this is the "tick Full Day / Half Day / Hourly"
    # part of the property form. Send one object per ticked type, e.g.
    # slots: [{slot_type: 'full_day', price: 25000}, {slot_type: 'hourly', price: 2000, min_duration_hours: 2}]
    slots = PropertySlotSerializer(many=True, required=False)
    primary_image = serializers.SerializerMethodField()
    total_bookings = serializers.SerializerMethodField()

    class Meta:
        model = Property
        fields = [
            'id', 'name', 'property_type', 'description', 'address',
            'location', 'google_map_link', 'capacity', 'security_deposit', 'status',
            'images', 'slots', 'primary_image', 'total_bookings',
            'created_at', 'updated_at',
        ]

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
        property_obj = Property.objects.create(**validated_data)
        for slot_data in slots_data:
            PropertySlot.objects.create(property=property_obj, **slot_data)
        return property_obj

    def update(self, instance, validated_data):
        slots_data = validated_data.pop('slots', None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        if slots_data is not None:
            # Full replace on update: simplest + safest since slots map
            # 1:1 to the ticked checkboxes on the property form. If you'd
            # rather diff/preserve existing rows by id, this is the spot
            # to change.
            instance.slots.all().delete()
            for slot_data in slots_data:
                PropertySlot.objects.create(property=instance, **slot_data)
        return instance


class PaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Payment
        fields = [
            'id', 'booking', 'amount', 'payment_method',
            'payment_date', 'reference', 'notes', 'received_at',
        ]
        read_only_fields = ['received_at']


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
        # total_amount/duration_hours are server-computed from the slot +
        # time range (see validate() and Booking.save()), not client-supplied.
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
            data['total_amount'] = round(float(property_slot.price) * duration, 2)
        else:
            # full_day / half_day: flat price regardless of the times entered.
            data['total_amount'] = property_slot.price

        # Overlap check (mirrors Booking.clean(), surfaced here so DRF
        # returns a clean 400 instead of an uncaught ValidationError).
        conflict_qs = Booking.objects.filter(
            property=property_obj,
            booking_date=booking_date,
            status__in=['reserved', 'booked', 'confirmed', 'occupied'],
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
    """Lightweight serializer for calendar view."""
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