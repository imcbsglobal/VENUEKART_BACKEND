from django.db import models
from django.contrib.auth.models import User as DjangoUser
from django.core.exceptions import ValidationError
from django.utils import timezone


# ---------------------------------------------------------------------------
# Client — one row per VenueKart customer (tenant)
# ---------------------------------------------------------------------------

class Client(models.Model):
    """
    A single tenant. Every Property / Booking / Payment row belongs to
    exactly one Client. The client_id here must match the client_id that
    the VenueKart activation API returns.
    """
    client_id = models.CharField(max_length=50, unique=True)   # e.g. "VC001"
    name = models.CharField(max_length=255)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=20, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['client_id']

    def __str__(self):
        return f"{self.client_id} – {self.name}"


# ---------------------------------------------------------------------------
# UserProfile — links a Django User to a Client (tenant) + stores role
# ---------------------------------------------------------------------------

class UserProfile(models.Model):
    ROLES = [
        ('super_admin', 'Super Admin'),
        ('admin',       'Admin'),
        ('staff',       'Staff'),
    ]

    user = models.OneToOneField(DjangoUser, on_delete=models.CASCADE, related_name='profile')
    client = models.ForeignKey(
        Client, on_delete=models.CASCADE, related_name='users',
        null=True, blank=True,   # null only for super_admin who spans all clients
    )
    role = models.CharField(max_length=20, choices=ROLES, default='admin')

    def __str__(self):
        client_str = self.client.client_id if self.client else 'ALL'
        return f"{self.user.username} [{client_str}] ({self.role})"

    @property
    def is_super_admin(self):
        return self.role == 'super_admin'


# ---------------------------------------------------------------------------
# Property
# ---------------------------------------------------------------------------

def _slugify_name(name, max_chars=8):
    """
    Convert an arbitrary name to a compact uppercase slug for use inside IDs.
    • Keeps only ASCII letters/digits (strips spaces, symbols, accents).
    • Upper-cases the result and caps at *max_chars* characters.
    Examples:
        "Raj Hall"  → "RAJHALL"
        "The Grand Ballroom" → "THEGRAND"
        "Villa #2"  → "VILLA2"
    """
    import re
    cleaned = re.sub(r'[^A-Za-z0-9]', '', name)
    return cleaned.upper()[:max_chars]


# Short type codes used inside Property IDs
PROPERTY_TYPE_CODES = {
    'auditorium': 'AUD',
    'house':      'HSE',
    'villa':      'VIL',
    'resort':     'RST',
    'plot':       'PLT',
    'commercial': 'COM',
}

# Short slot-type codes used inside Slot IDs
SLOT_TYPE_CODES = {
    'full_day': 'FD',
    'half_day': 'HD',
    'hourly':   'HR',
}


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

    # ── tenant scope ──────────────────────────────────────────────────────────
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name='properties', null=True)

    # ── human-readable unique identifier ─────────────────────────────────────
    # Format: {CLIENT_ID}-{TYPE_CODE}-{NNN}
    # Example: VC001-AUD-001
    property_id = models.CharField(max_length=30, unique=True, blank=True, editable=False)

    name = models.CharField(max_length=255)
    property_type = models.CharField(max_length=50, choices=PROPERTY_TYPES)
    description = models.TextField(blank=True)
    address = models.TextField()
    location = models.CharField(max_length=255)
    google_map_link = models.URLField(blank=True)
    capacity = models.PositiveIntegerField()
    security_deposit = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = 'Properties'
        ordering = ['name']

    def _generate_property_id(self):
        """
        Build a unique property_id of the form:
            {CLIENT_ID}-{TYPE_CODE}-{NNN}
        e.g.  VC001-AUD-001

        NNN is the next sequential number within the same client + type
        combination (padded to 3 digits).
        """
        client_part = self.client.client_id if self.client else 'GEN'
        type_code   = PROPERTY_TYPE_CODES.get(self.property_type, self.property_type[:3].upper())

        prefix = f"{client_part}-{type_code}-"
        existing = (
            Property.objects
            .filter(property_id__startswith=prefix)
            .exclude(pk=self.pk)
            .values_list('property_id', flat=True)
        )
        max_seq = 0
        for pid in existing:
            try:
                seq = int(pid.split('-')[-1])
                if seq > max_seq:
                    max_seq = seq
            except (ValueError, IndexError):
                pass
        return f"{prefix}{max_seq + 1:03d}"

    def save(self, *args, **kwargs):
        if not self.property_id:
            self.property_id = self._generate_property_id()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

    @property
    def offered_slot_types(self):
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

    # ── human-readable unique identifier ─────────────────────────────────────
    # Format: {CLIENT_ID}-{PROP_SLUG}-{SLOT_CODE}-{NN}
    # Example: VC001-RAJHALL-FD-01
    slot_id = models.CharField(max_length=40, unique=True, blank=True, editable=False)

    slot_type = models.CharField(max_length=20, choices=SLOT_TYPES)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    min_duration_hours = models.DecimalField(max_digits=4, decimal_places=1, default=1)
    max_duration_hours = models.DecimalField(max_digits=4, decimal_places=1, null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    created_at = models.DateTimeField(auto_now_add=True)

    def _generate_slot_id(self):
        """
        Build a unique slot_id of the form:
            {CLIENT_ID}-{PROP_SLUG}-{SLOT_CODE}-{NN}
        e.g.  VC001-RAJHALL-FD-01

        NN is the next sequential number within the same property + slot_type
        combination (padded to 2 digits).
        """
        prop   = self.property
        client_part = prop.client.client_id if prop.client else 'GEN'
        prop_slug   = _slugify_name(prop.name, max_chars=8)
        slot_code   = SLOT_TYPE_CODES.get(self.slot_type, self.slot_type[:2].upper())

        prefix = f"{client_part}-{prop_slug}-{slot_code}-"
        existing = (
            PropertySlot.objects
            .filter(slot_id__startswith=prefix)
            .exclude(pk=self.pk)
            .values_list('slot_id', flat=True)
        )
        max_seq = 0
        for sid in existing:
            try:
                seq = int(sid.split('-')[-1])
                if seq > max_seq:
                    max_seq = seq
            except (ValueError, IndexError):
                pass
        return f"{prefix}{max_seq + 1:02d}"

    def save(self, *args, **kwargs):
        if not self.slot_id:
            self.slot_id = self._generate_slot_id()
        super().save(*args, **kwargs)

    class Meta:
        ordering = ['property', 'slot_type']

    def __str__(self):
        return f"{self.property.name} – {self.get_slot_type_display()} (₹{self.price})"


# ---------------------------------------------------------------------------
# Booking
# ---------------------------------------------------------------------------

class Booking(models.Model):
    BOOKING_STATUS = [
        ('reserved', 'Reserved'),
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

    # ── tenant scope ──────────────────────────────────────────────────────────
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name='bookings', null=True)

    booking_number = models.CharField(max_length=20, unique=True, editable=False)
    property = models.ForeignKey(Property, on_delete=models.PROTECT, related_name='bookings')
    property_slot = models.ForeignKey(PropertySlot, on_delete=models.PROTECT, related_name='bookings')

    customer_name = models.CharField(max_length=255)
    mobile_number = models.CharField(max_length=20)

    event_name = models.CharField(max_length=255)
    event_type = models.CharField(max_length=50, choices=EVENT_TYPES)

    booking_date = models.DateField()
    start_time = models.TimeField()
    end_time = models.TimeField()
    duration_hours = models.DecimalField(max_digits=4, decimal_places=1, editable=False, default=0)

    total_amount = models.DecimalField(max_digits=10, decimal_places=2)
    advance_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    balance_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    payment_status = models.CharField(max_length=20, choices=PAYMENT_STATUS, default='pending')

    notes = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=BOOKING_STATUS, default='reserved')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        if not self.booking_number:
            last = Booking.objects.order_by('-id').first()
            next_id = (last.id + 1) if last else 1
            self.booking_number = f"BK{timezone.now().strftime('%Y%m')}{next_id:04d}"

        if self.start_time and self.end_time:
            start_secs = self.start_time.hour * 3600 + self.start_time.minute * 60 + self.start_time.second
            end_secs = self.end_time.hour * 3600 + self.end_time.minute * 60 + self.end_time.second
            self.duration_hours = round((end_secs - start_secs) / 3600, 1)

        self.balance_amount = self.total_amount - self.advance_amount

        if self.advance_amount <= 0:
            self.payment_status = 'pending'
        elif self.balance_amount <= 0:
            self.payment_status = 'paid'
        else:
            self.payment_status = 'partial'

        super().save(*args, **kwargs)

    def clean(self):
        if self.booking_date and self.property_id and self.start_time and self.end_time:
            conflict_qs = Booking.objects.filter(
                property_id=self.property_id,
                booking_date=self.booking_date,
                status__in=['reserved'],
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


# ---------------------------------------------------------------------------
# Payment
# ---------------------------------------------------------------------------

class Payment(models.Model):
    PAYMENT_METHODS = [
        ('cash', 'Cash'),
        ('bank_transfer', 'Bank Transfer'),
        ('upi', 'UPI'),
        ('cheque', 'Cheque'),
        ('other', 'Other'),
    ]

    # ── tenant scope (denormalised from booking.client for fast filtering) ───
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name='payments', null=True)

    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name='payments')
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    payment_method = models.CharField(max_length=30, choices=PAYMENT_METHODS, default='cash')
    payment_date = models.DateField(default=timezone.localdate)
    reference = models.CharField(max_length=100, blank=True)
    notes = models.TextField(blank=True)
    received_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        # Inherit client from the booking automatically so callers don't
        # have to pass it explicitly.
        if not self.client_id:
            self.client = self.booking.client
        is_new = self.pk is None
        super().save(*args, **kwargs)
        if is_new:
            booking = self.booking
            booking.advance_amount = (booking.advance_amount or 0) + self.amount
            booking.save()

    def delete(self, *args, **kwargs):
        booking = self.booking
        amount = self.amount
        super().delete(*args, **kwargs)
        booking.advance_amount = max(booking.advance_amount - amount, 0)
        booking.save()

    def __str__(self):
        return f"{self.booking.booking_number} – ₹{self.amount}"


# ---------------------------------------------------------------------------
# Enquiry — lightweight lead capture; no payment, no slot conflict lock
# ---------------------------------------------------------------------------

class Enquiry(models.Model):
    ENQUIRY_STATUS = [
        ('enquiry', 'Enquiry'),
        ('reserved', 'Reserved'),
    ]
    EVENT_TYPES = Booking.EVENT_TYPES  # reuse same list

    # ── tenant scope ──────────────────────────────────────────────────────────
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name='enquiries', null=True)

    enquiry_number = models.CharField(max_length=20, unique=True, editable=False)
    property = models.ForeignKey(Property, on_delete=models.PROTECT, related_name='enquiries')
    property_slot = models.ForeignKey(
        PropertySlot, on_delete=models.PROTECT, related_name='enquiries',
        null=True, blank=True,
    )

    customer_name = models.CharField(max_length=255)
    mobile_number = models.CharField(max_length=20)
    email = models.EmailField(blank=True)

    event_name = models.CharField(max_length=255, blank=True)
    event_type = models.CharField(max_length=50, choices=EVENT_TYPES, blank=True)

    enquiry_date = models.DateField(null=True, blank=True)   # prospective event date
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)

    notes = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=ENQUIRY_STATUS, default='enquiry')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # When the enquiry is promoted to a booking, we store the FK here.
    booking = models.OneToOneField(
        Booking, on_delete=models.SET_NULL,
        related_name='enquiry', null=True, blank=True,
    )

    class Meta:
        ordering = ['-created_at']
        verbose_name_plural = 'Enquiries'

    def save(self, *args, **kwargs):
        if not self.enquiry_number:
            last = Enquiry.objects.order_by('-id').first()
            next_id = (last.id + 1) if last else 1
            self.enquiry_number = f"ENQ{timezone.now().strftime('%Y%m')}{next_id:04d}"
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.enquiry_number} – {self.customer_name} @ {self.property.name}"