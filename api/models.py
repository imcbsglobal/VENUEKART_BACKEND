from django.db import models
from django.core.exceptions import ValidationError
from django.utils import timezone


class Property(models.Model):
    PROPERTY_TYPES = [
        ('auditorium', 'Auditorium'),
        ('house', 'House'),
        ('villa', 'Villa'),
        ('resort', 'Resort'),
        ('plot', 'Plot'),
        ('commercial', 'Commercial Building'),
    ]
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('inactive', 'Inactive'),
    ]

    name = models.CharField(max_length=255)
    property_type = models.CharField(max_length=50, choices=PROPERTY_TYPES)
    description = models.TextField(blank=True)
    address = models.TextField()
    location = models.CharField(max_length=255)
    google_map_link = models.URLField(blank=True)
    capacity = models.PositiveIntegerField()
    # NOTE: rent_type / rent_amount removed. Pricing now lives per slot type
    # on PropertySlot below, since each ticked type (Full Day / Half Day /
    # Hourly) has its own price.
    security_deposit = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = 'Properties'
        ordering = ['name']

    def __str__(self):
        return self.name

    @property
    def offered_slot_types(self):
        """Distinct slot types this property currently offers, e.g. ['full_day', 'hourly']."""
        return list(self.slots.filter(status='active').values_list('slot_type', flat=True).distinct())


class PropertyImage(models.Model):
    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='images')
    image = models.ImageField(upload_to='property_images/')
    is_primary = models.BooleanField(default=False)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if self.is_primary:
            PropertyImage.objects.filter(property=self.property, is_primary=True).update(is_primary=False)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.property.name} - {'Primary' if self.is_primary else 'Image'}"


class PropertySlot(models.Model):
    """
    Replaces the old global `Slot` model. A slot now belongs to exactly one
    Property — ticking "Full Day" / "Half Day" / "Hourly" on the property
    form just creates one of these rows with its own price. There's no
    fixed time window stored here; the actual start/end time for a booking
    is entered directly on the booking itself.

    `price` is a flat fee for full_day/half_day, and PER HOUR for hourly.
    """
    SLOT_TYPES = [
        ('full_day', 'Full Day'),
        ('half_day', 'Half Day'),
        ('hourly', 'Hourly'),
    ]
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('inactive', 'Inactive'),
    ]

    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='slots')
    slot_type = models.CharField(max_length=20, choices=SLOT_TYPES)
    price = models.DecimalField(max_digits=10, decimal_places=2)

    # Hourly-only constraints on the custom duration a customer can pick.
    min_duration_hours = models.DecimalField(max_digits=4, decimal_places=1, default=1)
    max_duration_hours = models.DecimalField(max_digits=4, decimal_places=1, null=True, blank=True)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['property', 'slot_type']

    def __str__(self):
        return f"{self.property.name} – {self.get_slot_type_display()} (₹{self.price})"


class Booking(models.Model):
    BOOKING_STATUS = [
        ('inquiry', 'Inquiry'),
        ('reserved', 'Reserved'),
        ('booked', 'Booked'),
        ('confirmed', 'Confirmed'),
        ('occupied', 'Occupied'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    ]
    EVENT_TYPES = [
        ('wedding', 'Wedding'),
        ('birthday', 'Birthday'),
        ('corporate', 'Corporate Event'),
        ('conference', 'Conference'),
        ('seminar', 'Seminar'),
        ('exhibition', 'Exhibition'),
        ('concert', 'Concert'),
        ('private', 'Private Function'),
        ('other', 'Other'),
    ]
    PAYMENT_STATUS = [
        ('pending', 'Pending'),
        ('partial', 'Partial Paid'),
        ('paid', 'Fully Paid'),
    ]

    booking_number = models.CharField(max_length=20, unique=True, editable=False)
    property = models.ForeignKey(Property, on_delete=models.PROTECT, related_name='bookings')
    property_slot = models.ForeignKey(PropertySlot, on_delete=models.PROTECT, related_name='bookings')

    # Customer Info
    customer_name = models.CharField(max_length=255)
    mobile_number = models.CharField(max_length=20)

    # Event Info
    event_name = models.CharField(max_length=255)
    event_type = models.CharField(max_length=50, choices=EVENT_TYPES)

    # Booking Details
    booking_date = models.DateField()
    # Actual booked time range. For full_day/half_day this is copied from
    # property_slot. For hourly this is the customer's custom selection
    # (validated to fall inside property_slot's window).
    start_time = models.TimeField()
    end_time = models.TimeField()
    duration_hours = models.DecimalField(max_digits=4, decimal_places=1, editable=False, default=0)

    # Payment
    total_amount = models.DecimalField(max_digits=10, decimal_places=2)
    advance_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    balance_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    payment_status = models.CharField(max_length=20, choices=PAYMENT_STATUS, default='pending')

    notes = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=BOOKING_STATUS, default='inquiry')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        # Auto-generate booking number
        if not self.booking_number:
            last = Booking.objects.order_by('-id').first()
            next_id = (last.id + 1) if last else 1
            self.booking_number = f"BK{timezone.now().strftime('%Y%m')}{next_id:04d}"

        # Auto-compute duration from the actual booked time range
        if self.start_time and self.end_time:
            start_secs = self.start_time.hour * 3600 + self.start_time.minute * 60 + self.start_time.second
            end_secs = self.end_time.hour * 3600 + self.end_time.minute * 60 + self.end_time.second
            self.duration_hours = round((end_secs - start_secs) / 3600, 1)

        # Auto-compute balance
        self.balance_amount = self.total_amount - self.advance_amount

        # Auto-compute payment status
        if self.advance_amount <= 0:
            self.payment_status = 'pending'
        elif self.balance_amount <= 0:
            self.payment_status = 'paid'
        else:
            self.payment_status = 'partial'

        super().save(*args, **kwargs)

    def clean(self):
        # Overlap-based double-booking validation. This replaces the old
        # "same slot id" check — now that Hourly bookings have custom,
        # non-fixed time ranges, two bookings can conflict even if they
        # picked different PropertySlot rows (or the same row at different
        # times shouldn't conflict at all), so we compare actual time
        # ranges for the same property + date instead.
        if self.booking_date and self.property_id and self.start_time and self.end_time:
            conflict_qs = Booking.objects.filter(
                property_id=self.property_id,
                booking_date=self.booking_date,
                status__in=['reserved', 'booked', 'confirmed', 'occupied'],
            ).exclude(
                models.Q(end_time__lte=self.start_time) | models.Q(start_time__gte=self.end_time)
            )
            if self.pk:
                conflict_qs = conflict_qs.exclude(pk=self.pk)
            if conflict_qs.exists():
                raise ValidationError(
                    "Booking cannot be created because it overlaps with an existing booking for this property."
                )

    def __str__(self):
        return f"{self.booking_number} – {self.customer_name} @ {self.property.name}"


class Payment(models.Model):
    PAYMENT_METHODS = [
        ('cash', 'Cash'),
        ('bank_transfer', 'Bank Transfer'),
        ('upi', 'UPI'),
        ('cheque', 'Cheque'),
        ('other', 'Other'),
    ]

    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name='payments')
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    payment_method = models.CharField(max_length=30, choices=PAYMENT_METHODS, default='cash')
    payment_date = models.DateField(default=timezone.localdate)
    reference = models.CharField(max_length=100, blank=True)
    notes = models.TextField(blank=True)
    received_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        super().save(*args, **kwargs)

        # Recording a NEW payment rolls it straight into the booking's
        # advance_amount, which re-triggers Booking.save()'s automatic
        # balance_amount / payment_status recalculation. Editing an
        # existing Payment row (e.g. fixing a typo in notes) intentionally
        # does NOT touch the booking total again here, so re-saving an old
        # payment can't double-count it.
        if is_new:
            booking = self.booking
            booking.advance_amount = (booking.advance_amount or 0) + self.amount
            booking.save()

    def delete(self, *args, **kwargs):
        booking = self.booking
        amount = self.amount
        super().delete(*args, **kwargs)

        # Mirror image of save(): removing a payment (e.g. deleting a
        # mistaken entry from the admin) gives the amount back to the
        # balance instead of leaving it permanently "paid".
        booking.advance_amount = max(booking.advance_amount - amount, 0)
        booking.save()

    def __str__(self):
        return f"{self.booking.booking_number} – ₹{self.amount}"