import calendar
import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta

from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User as DjangoUser
from django.db.models import Sum, Count, Q
from django.utils import timezone

from rest_framework import viewsets, status, permissions
from rest_framework.authtoken.models import Token
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Client, UserProfile, Property, PropertyImage, PropertySlot, Booking, Payment, Enquiry
from .serializers import (
    ClientSerializer,
    UserSerializer,
    CreateUserSerializer,
    PropertySerializer,
    PropertyImageSerializer,
    PropertySlotSerializer,
    BookingListSerializer,
    BookingDetailSerializer,
    PaymentSerializer,
    DashboardSerializer,
    EnquirySerializer,
)


# ---------------------------------------------------------------------------
# Helpers — resolving tenant from the authenticated request
# ---------------------------------------------------------------------------

def _get_client(request):
    """
    Returns the Client associated with the logged-in user's profile.
    Super-admins have client=None (they can see all tenants) — callers must
    handle that case where cross-tenant queries are needed.
    """
    try:
        return request.user.profile.client
    except UserProfile.DoesNotExist:
        return None


def _is_super(request):
    try:
        return request.user.profile.role == 'super_admin'
    except UserProfile.DoesNotExist:
        return request.user.is_superuser


def _client_scope(qs, request, field='client'):
    """
    Filters a queryset to the logged-in user's client unless the user is
    a super_admin (who can see everything). Pass field='client' for direct
    FK fields, or 'booking__client' for Payment-level filtering.
    """
    if _is_super(request):
        return qs
    client = _get_client(request)
    if client is None:
        return qs.none()
    return qs.filter(**{field: client})


# ---------------------------------------------------------------------------
# Permission helpers
# ---------------------------------------------------------------------------

class IsSuperAdmin(permissions.BasePermission):
    """Only super_admin role can access."""
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and _is_super(request))


# ---------------------------------------------------------------------------
# Client management (super_admin only)
# ---------------------------------------------------------------------------

class ClientViewSet(viewsets.ModelViewSet):
    """
    CRUD for Client (tenant) records.
    Only super_admin can reach these endpoints.

    GET    /api/clients/          list all tenants
    POST   /api/clients/          create a new tenant
    GET    /api/clients/<id>/     retrieve one tenant
    PUT    /api/clients/<id>/     update
    DELETE /api/clients/<id>/     delete (careful — cascades to all their data)
    """
    queryset = Client.objects.all().order_by('client_id')
    serializer_class = ClientSerializer
    permission_classes = [permissions.IsAuthenticated, IsSuperAdmin]


# ---------------------------------------------------------------------------
# User management (super_admin only)
# ---------------------------------------------------------------------------

class UserViewSet(viewsets.ModelViewSet):
    """
    List / create / deactivate users.

    GET  /api/users/          list users (super_admin sees all; others see their client)
    POST /api/users/          create a user (super_admin only)
    GET  /api/users/<id>/     retrieve
    PATCH /api/users/<id>/    update (role / active flag)
    DELETE /api/users/<id>/   delete

    PATCH /api/users/<id>/toggle-active/   flip is_active
    """
    queryset = DjangoUser.objects.select_related('profile', 'profile__client').order_by('username')
    permission_classes = [permissions.IsAuthenticated, IsSuperAdmin]

    def get_serializer_class(self):
        if self.action == 'create':
            return CreateUserSerializer
        return UserSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        # Optional: super_admin can filter by client_id query param
        client_id = self.request.query_params.get('client_id')
        if client_id:
            qs = qs.filter(profile__client__client_id=client_id.upper())
        return qs

    def create(self, request, *args, **kwargs):
        serializer = CreateUserSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(UserSerializer(user).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['patch'], url_path='toggle-active')
    def toggle_active(self, request, pk=None):
        user = self.get_object()
        user.is_active = not user.is_active
        user.save()
        return Response(UserSerializer(user).data)


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

class PropertyViewSet(viewsets.ModelViewSet):
    queryset = Property.objects.all().prefetch_related('images', 'slots')
    serializer_class = PropertySerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = _client_scope(super().get_queryset(), self.request)

        status_param = self.request.query_params.get('status')
        property_type = self.request.query_params.get('property_type')
        search = self.request.query_params.get('search')

        if status_param:
            qs = qs.filter(status=status_param)
        if property_type:
            qs = qs.filter(property_type=property_type)
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(location__icontains=search))
        return qs

    def perform_create(self, serializer):
        # Inject the tenant automatically so the frontend never has to send it
        client = _get_client(self.request)
        if client is None and not _is_super(self.request):
            raise permissions.PermissionDenied("No client profile attached to your account.")
        # Super-admin can pass client explicitly; regular users get theirs injected
        if client:
            serializer.save(client=client)
        else:
            # super_admin must supply client_id in request body
            client_id = self.request.data.get('client_id', '').strip().upper()
            try:
                supplied_client = Client.objects.get(client_id=client_id)
            except Client.DoesNotExist:
                from rest_framework.exceptions import ValidationError
                raise ValidationError({"client_id": f"Client '{client_id}' not found."})
            serializer.save(client=supplied_client)

    @action(detail=True, methods=['get'])
    def slots(self, request, pk=None):
        property_obj = self.get_object()
        slots_qs = property_obj.slots.filter(status='active')
        return Response(PropertySlotSerializer(slots_qs, many=True).data)

    @action(detail=True, methods=['post'], url_path='upload-image')
    def upload_image(self, request, pk=None):
        property_obj = self.get_object()
        image = request.FILES.get('image')
        if not image:
            return Response({'error': 'No image provided.'}, status=status.HTTP_400_BAD_REQUEST)
        is_primary = request.data.get('is_primary') in [True, 'true', 'True', '1']
        img = PropertyImage.objects.create(property=property_obj, image=image, is_primary=is_primary)
        return Response(PropertyImageSerializer(img).data, status=status.HTTP_201_CREATED)


# ---------------------------------------------------------------------------
# Bookings
# ---------------------------------------------------------------------------

class BookingViewSet(viewsets.ModelViewSet):
    queryset = Booking.objects.select_related('property', 'property_slot').prefetch_related('payments')
    permission_classes = [permissions.IsAuthenticated]

    def get_serializer_class(self):
        if self.action == 'list':
            return BookingListSerializer
        return BookingDetailSerializer

    def get_queryset(self):
        qs = _client_scope(super().get_queryset(), self.request)

        # Double-lock: also ensure the booking's property belongs to the same
        # client. This filters out any stale cross-tenant bookings that were
        # created before the property-ownership guard was added.
        if not _is_super(self.request):
            client = _get_client(self.request)
            if client:
                qs = qs.filter(property__client=client)

        status_param = self.request.query_params.get('status')
        property_id  = self.request.query_params.get('property')
        date_from    = self.request.query_params.get('date_from')
        date_to      = self.request.query_params.get('date_to')
        search       = self.request.query_params.get('search')

        if status_param:
            qs = qs.filter(status=status_param)
        if property_id:
            qs = qs.filter(property_id=property_id)
        if date_from:
            qs = qs.filter(booking_date__gte=date_from)
        if date_to:
            qs = qs.filter(booking_date__lte=date_to)
        if search:
            qs = qs.filter(
                Q(booking_number__icontains=search)
                | Q(customer_name__icontains=search)
                | Q(mobile_number__icontains=search)
                | Q(event_name__icontains=search)
            )
        return qs

    def perform_create(self, serializer):
        from rest_framework.exceptions import ValidationError as DRFValidationError

        client = _get_client(self.request)
        if client is None and not _is_super(self.request):
            raise permissions.PermissionDenied("No client profile attached to your account.")

        # ── Resolve which client will own this booking ────────────────────────
        if client:
            booking_client = client
        else:
            # super_admin must supply client_id in request body
            client_id = self.request.data.get('client_id', '').strip().upper()
            try:
                booking_client = Client.objects.get(client_id=client_id)
            except Client.DoesNotExist:
                raise DRFValidationError({"client_id": f"Client '{client_id}' not found."})

        # ── Guard: the chosen property must belong to the same client ─────────
        property_id = self.request.data.get('property')
        if property_id:
            from .models import Property as PropertyModel
            try:
                prop = PropertyModel.objects.get(pk=property_id)
            except PropertyModel.DoesNotExist:
                raise DRFValidationError({"property": "Property not found."})

            if not _is_super(self.request) and prop.client_id != booking_client.id:
                raise DRFValidationError(
                    {"property": "You can only create bookings for properties belonging to your account."}
                )

        serializer.save(client=booking_client)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        out = BookingListSerializer(serializer.instance, context=self.get_serializer_context())
        headers = self.get_success_headers(out.data)
        return Response(out.data, status=status.HTTP_201_CREATED, headers=headers)

    # ── Availability pre-check ────────────────────────────────────────────────
    @action(detail=False, methods=['post'], url_path='check-availability')
    def check_availability(self, request):
        """
        POST /api/bookings/check-availability/

        Pre-flight availability check. Call this before submitting the booking
        form to confirm the slot is free and receive the computed total_amount.

        Request body:
        {
            "property":      <int>   — property pk,
            "property_slot": <int>   — slot pk,
            "booking_date":  "YYYY-MM-DD",
            "start_time":    "HH:MM",
            "end_time":      "HH:MM"
        }

        Success response  (HTTP 200):
        {
            "available":       true,
            "property":        <int>,
            "property_name":   "Raj Hall",
            "property_type":   "auditorium",
            "slot_type":       "hourly",
            "slot_type_label": "Hourly",
            "booking_date":    "2025-08-15",
            "start_time":      "10:00",
            "end_time":        "14:00",
            "duration_hours":  4.0,
            "total_amount":    "8000.00",
            "reason":          ""
        }

        Failure response  (HTTP 200 with available=false  OR  4xx):
        {
            "available": false,
            "reason":    "This Hourly slot is already reserved on 2025-08-15 (booking BK202508XXXX)."
        }
        """
        from decimal import Decimal as D

        property_id  = request.data.get('property')
        slot_id      = request.data.get('property_slot')
        date_str     = request.data.get('booking_date')
        start_str    = request.data.get('start_time')
        end_str      = request.data.get('end_time')

        # ── 1. Required-field check ───────────────────────────────────────────
        missing = [
            f for f, v in [
                ('property',      property_id),
                ('property_slot', slot_id),
                ('booking_date',  date_str),
                ('start_time',    start_str),
                ('end_time',      end_str),
            ] if not v
        ]
        if missing:
            return Response(
                {'available': False, 'reason': f"Missing required fields: {', '.join(missing)}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # ── 2. Parse date / times ─────────────────────────────────────────────
        try:
            booking_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            return Response(
                {'available': False, 'reason': 'booking_date must be in YYYY-MM-DD format.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            start = datetime.strptime(start_str, '%H:%M').time()
            end   = datetime.strptime(end_str,   '%H:%M').time()
        except ValueError:
            return Response(
                {'available': False, 'reason': 'start_time and end_time must be in HH:MM format.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if end <= start:
            return Response(
                {'available': False, 'reason': 'end_time must be after start_time.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # ── 3. Fetch property (tenant-scoped) ─────────────────────────────────
        try:
            prop_qs = Property.objects.all()
            if not _is_super(request):
                prop_qs = prop_qs.filter(client=_get_client(request))
            property_obj = prop_qs.get(pk=property_id)
        except Property.DoesNotExist:
            return Response(
                {'available': False, 'reason': 'Property not found.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        # ── 4. Fetch slot ─────────────────────────────────────────────────────
        try:
            slot = PropertySlot.objects.get(pk=slot_id, property=property_obj, status='active')
        except PropertySlot.DoesNotExist:
            return Response(
                {'available': False, 'reason': 'Slot not found or inactive for this property.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        # ── 5. Duration + total_amount calculation ────────────────────────────
        if slot.slot_type == 'hourly':
            duration_secs = (
                end.hour * 3600 + end.minute * 60
                - start.hour * 3600 - start.minute * 60
            )
            duration = duration_secs / 3600

            if duration < float(slot.min_duration_hours):
                return Response({
                    'available': False,
                    'reason': (
                        f"Minimum booking duration for this slot is "
                        f"{slot.min_duration_hours} hour(s)."
                    ),
                }, status=status.HTTP_400_BAD_REQUEST)

            if slot.max_duration_hours and duration > float(slot.max_duration_hours):
                return Response({
                    'available': False,
                    'reason': (
                        f"Maximum booking duration for this slot is "
                        f"{slot.max_duration_hours} hour(s)."
                    ),
                }, status=status.HTTP_400_BAD_REQUEST)

            total_amount = D(str(round(float(slot.price) * duration, 2)))
        else:
            duration     = None
            total_amount = slot.price

        # ── 6. Double-booking checks (mirrors BookingDetailSerializer.validate) ─
        day_qs = Booking.objects.filter(
            property=property_obj,
            booking_date=booking_date,
        ).exclude(status='cancelled')

        def _overlapping(qs):
            return qs.exclude(Q(end_time__lte=start) | Q(start_time__gte=end))

        # Rule 1 — existing full-day booking locks the whole property
        if day_qs.filter(property_slot__slot_type='full_day').exists():
            return Response({
                'available': False,
                'reason': (
                    f"'{property_obj.name}' is fully booked on {booking_date}. "
                    f"No other booking can be made on this date."
                ),
            })

        # Rule 2 — can't add a full-day booking if any booking already exists
        if slot.slot_type == 'full_day' and day_qs.exists():
            return Response({
                'available': False,
                'reason': (
                    f"'{property_obj.name}' already has a booking on {booking_date}. "
                    f"A full-day booking is not possible."
                ),
            })

        # Rule 3 — exact slot clash
        slot_qs    = day_qs.filter(property_slot=slot)
        slot_clash = (
            slot_qs if slot.slot_type in ('full_day', 'half_day')
            else _overlapping(slot_qs)
        )
        existing = slot_clash.first()
        if existing:
            return Response({
                'available': False,
                'reason': (
                    f"This {slot.get_slot_type_display()} slot is already reserved on "
                    f"{booking_date} (booking {existing.booking_number}). "
                    f"Please choose a different slot or date."
                ),
            })

        # Rule 4 — catch-all time overlap with any other slot on the same property
        time_clash = _overlapping(day_qs).first()
        if time_clash:
            return Response({
                'available': False,
                'reason': (
                    f"The selected time window overlaps with an existing booking on "
                    f"{booking_date} "
                    f"({time_clash.start_time.strftime('%H:%M')}–"
                    f"{time_clash.end_time.strftime('%H:%M')}, "
                    f"booking {time_clash.booking_number})."
                ),
            })

        # ── 7. All clear ──────────────────────────────────────────────────────
        return Response({
            'available':       True,
            'reason':          '',
            'property':        property_obj.id,
            'property_name':   property_obj.name,
            'property_type':   property_obj.property_type,
            'slot_type':       slot.slot_type,
            'slot_type_label': slot.get_slot_type_display(),
            'booking_date':    date_str,
            'start_time':      start_str,
            'end_time':        end_str,
            'duration_hours':  duration,
            'total_amount':    str(total_amount),
        })

    @action(detail=True, methods=['patch'], url_path='status')
    def update_status(self, request, pk=None):
        booking = self.get_object()
        new_status = request.data.get('status')
        valid_statuses = [c[0] for c in Booking.BOOKING_STATUS]
        if new_status not in valid_statuses:
            return Response(
                {'error': f"Invalid status. Must be one of: {', '.join(valid_statuses)}."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        booking.status = new_status
        booking.save()
        return Response(BookingDetailSerializer(booking).data)

    @action(detail=True, methods=['post'])
    def add_payment(self, request, pk=None):
        booking = self.get_object()
        serializer = PaymentSerializer(data={**request.data, 'booking': booking.id})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        booking.refresh_from_db()
        return Response(BookingDetailSerializer(booking).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        booking = self.get_object()
        booking.status = 'cancelled'
        booking.save()
        return Response(BookingDetailSerializer(booking).data)


# ---------------------------------------------------------------------------
# Enquiries
# ---------------------------------------------------------------------------

class EnquiryViewSet(viewsets.ModelViewSet):
    """
    CRUD for Enquiries (lightweight leads — no payment, no slot lock).

    GET    /api/enquiries/                   list (filterable by status, property, search)
    POST   /api/enquiries/                   create a new enquiry
    GET    /api/enquiries/<id>/              retrieve
    PATCH  /api/enquiries/<id>/              update
    DELETE /api/enquiries/<id>/              delete

    PATCH  /api/enquiries/<id>/promote/      promote enquiry → Booking (status=reserved)
    """
    queryset = Enquiry.objects.select_related('property', 'property_slot', 'booking').all()
    serializer_class = EnquirySerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = _client_scope(super().get_queryset(), self.request, field='client')

        status_param = self.request.query_params.get('status')
        property_id  = self.request.query_params.get('property')
        search       = self.request.query_params.get('search')

        if status_param:
            qs = qs.filter(status=status_param)
        if property_id:
            qs = qs.filter(property_id=property_id)
        if search:
            qs = qs.filter(
                Q(enquiry_number__icontains=search)
                | Q(customer_name__icontains=search)
                | Q(mobile_number__icontains=search)
                | Q(event_name__icontains=search)
            )
        return qs

    def perform_create(self, serializer):
        from rest_framework.exceptions import ValidationError as DRFValidationError
        client = _get_client(self.request)
        if client is None and not _is_super(self.request):
            raise permissions.PermissionDenied("No client profile attached to your account.")
        if client:
            serializer.save(client=client)
        else:
            client_id = self.request.data.get('client_id', '').strip().upper()
            try:
                supplied_client = Client.objects.get(client_id=client_id)
            except Client.DoesNotExist:
                raise DRFValidationError({"client_id": f"Client '{client_id}' not found."})
            serializer.save(client=supplied_client)

    @action(detail=True, methods=['patch'], url_path='promote')
    def promote(self, request, pk=None):
        """
        Promote an enquiry to a confirmed Booking.
        The enquiry must have property, property_slot, enquiry_date,
        start_time and end_time set — all required for a proper Booking.
        On success: creates the Booking (status=reserved), links it back to
        the enquiry, and sets enquiry.status = 'reserved'.
        """
        from rest_framework.exceptions import ValidationError as DRFValidationError
        from decimal import Decimal

        enquiry = self.get_object()

        if enquiry.status == 'reserved':
            return Response(
                {'detail': 'This enquiry has already been promoted to a booking.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        missing = [f for f, v in [
            ('property_slot', enquiry.property_slot_id),
            ('enquiry_date',  enquiry.enquiry_date),
            ('start_time',    enquiry.start_time),
            ('end_time',      enquiry.end_time),
        ] if not v]
        if missing:
            return Response(
                {'detail': f"Cannot promote: missing fields — {', '.join(missing)}. Update the enquiry first."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        slot  = enquiry.property_slot
        start = enquiry.start_time
        end   = enquiry.end_time

        if end <= start:
            return Response(
                {'detail': 'end_time must be after start_time.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Calculate total amount
        if slot.slot_type == 'hourly':
            duration = (
                end.hour * 3600 + end.minute * 60
                - start.hour * 3600 - start.minute * 60
            ) / 3600
            total = Decimal(str(round(float(slot.price) * duration, 2)))
        else:
            total = slot.price

        # Check for conflicts
        conflict = Booking.objects.filter(
            property=enquiry.property,
            booking_date=enquiry.enquiry_date,
            status__in=['reserved'],
        ).exclude(
            Q(end_time__lte=start) | Q(start_time__gte=end)
        )
        if conflict.exists():
            return Response(
                {'detail': 'Cannot promote: the slot is already reserved for another booking at that time.'},
                status=status.HTTP_409_CONFLICT,
            )

        booking_client = enquiry.client
        if booking_client is None and not _is_super(request):
            raise permissions.PermissionDenied("No client profile attached to this enquiry.")

        booking = Booking.objects.create(
            client=booking_client,
            property=enquiry.property,
            property_slot=slot,
            customer_name=enquiry.customer_name,
            mobile_number=enquiry.mobile_number,
            event_name=enquiry.event_name or enquiry.customer_name,
            event_type=enquiry.event_type or 'other',
            booking_date=enquiry.enquiry_date,
            start_time=start,
            end_time=end,
            total_amount=total,
            advance_amount=Decimal('0'),
            notes=enquiry.notes,
            status='reserved',
        )

        enquiry.booking = booking
        enquiry.status  = 'reserved'
        enquiry.save(update_fields=['booking', 'status', 'updated_at'])

        return Response({
            'detail':  'Enquiry promoted to booking successfully.',
            'booking': BookingListSerializer(booking).data,
            'enquiry': EnquirySerializer(enquiry).data,
        }, status=status.HTTP_201_CREATED)


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------

class PaymentViewSet(viewsets.ModelViewSet):
    queryset = Payment.objects.select_related('booking').order_by('received_at')
    serializer_class = PaymentSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = _client_scope(super().get_queryset(), self.request)
        booking_id = self.request.query_params.get('booking')
        if booking_id:
            qs = qs.filter(booking_id=booking_id)
        return qs


# ---------------------------------------------------------------------------
# Availability check
# ---------------------------------------------------------------------------

class AvailabilityCheckView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        property_id = request.query_params.get('property')
        date_str    = request.query_params.get('date')

        if not property_id or not date_str:
            return Response(
                {'error': 'property and date are required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            booking_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            return Response(
                {'error': 'date must be in YYYY-MM-DD format.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            qs = Property.objects.all()
            if not _is_super(request):
                client = _get_client(request)
                qs = qs.filter(client=client)
            property_obj = qs.get(pk=property_id)
        except Property.DoesNotExist:
            return Response({'error': 'Property not found.'}, status=status.HTTP_404_NOT_FOUND)

        existing = Booking.objects.filter(
            property=property_obj,
            booking_date=booking_date,
            status__in=['reserved'],
        ).order_by('start_time')

        busy_ranges = [
            {
                'start':          b.start_time.strftime('%H:%M'),
                'end':            b.end_time.strftime('%H:%M'),
                'booking_number': b.booking_number,
                'customer_name':  b.customer_name,
            }
            for b in existing
        ]

        return Response({
            'property':    property_obj.id,
            'date':        date_str,
            'busy_ranges': busy_ranges,
        })


class DateAvailabilityView(APIView):
    """
    Date-first booking flow.

    GET /availability/?date=YYYY-MM-DD

    Returns every active property the user may book, each with its slots and a
    per-slot `available` flag for that date. Mirrors the double-booking rules in
    BookingDetailSerializer.validate():
      • A full-day booking locks the whole property for the date.
      • A full-day slot is only bookable on a completely empty date.
      • A half-day slot is unavailable once that exact slot is taken that date.
      • Hourly slots stay listed (the exact time window is validated on submit).
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        date_str = request.query_params.get('date')
        if not date_str:
            return Response({'error': 'date is required.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            booking_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            return Response(
                {'error': 'date must be in YYYY-MM-DD format.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        props = Property.objects.filter(status='active').prefetch_related('slots')
        if not _is_super(request):
            client = _get_client(request)
            props = props.filter(client=client)

        # Non-cancelled bookings on this date, grouped per property.
        booked = (
            Booking.objects
            .filter(booking_date=booking_date)
            .exclude(status='cancelled')
            .values('property_id', 'property_slot_id', 'property_slot__slot_type')
        )
        by_prop = {}
        for b in booked:
            entry = by_prop.setdefault(
                b['property_id'], {'has_full_day': False, 'slot_ids': set(), 'count': 0}
            )
            entry['count'] += 1
            entry['slot_ids'].add(b['property_slot_id'])
            if b['property_slot__slot_type'] == 'full_day':
                entry['has_full_day'] = True

        out = []
        for p in props:
            info = by_prop.get(p.id, {'has_full_day': False, 'slot_ids': set(), 'count': 0})
            slots_out = []
            for s in p.slots.all():
                if s.status != 'active':
                    continue
                available, reason = True, ''
                if info['has_full_day']:
                    available, reason = False, 'Property is booked for the full day'
                elif s.slot_type == 'full_day' and info['count'] > 0:
                    available, reason = False, 'Date already has bookings'
                elif s.slot_type == 'half_day' and s.id in info['slot_ids']:
                    available, reason = False, 'This slot is already booked'
                slots_out.append({
                    'id':              s.id,
                    'slot_type':       s.slot_type,
                    'slot_type_label': s.get_slot_type_display(),
                    'price':           str(s.price),
                    'available':       available,
                    'reason':          reason,
                })
            out.append({
                'id':            p.id,
                'name':          p.name,
                'property_type': p.property_type,
                'location':      p.location,
                'available':     any(sl['available'] for sl in slots_out),
                'slots':         slots_out,
            })

        return Response({'date': date_str, 'properties': out})


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------

STATUS_COLORS = {
    'reserved':  '#F59E0B',
    'completed': '#10B981',
    'cancelled': '#374151',
    'enquiry':   '#6B7280',
}


class CalendarView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        today      = timezone.now().date()
        month      = int(request.query_params.get('month', today.month))
        year       = int(request.query_params.get('year',  today.year))
        property_id = request.query_params.get('property')

        qs = _client_scope(
            Booking.objects.filter(
                booking_date__year=year,
                booking_date__month=month,
            ).exclude(status='cancelled').select_related('property', 'property_slot'),
            request,
        )

        if property_id:
            qs = qs.filter(property_id=property_id)

        events = []
        for b in qs:
            events.append({
                'id':              b.id,
                'title':           f"{b.booking_number} — {b.customer_name}",
                'start':           datetime.combine(b.booking_date, b.start_time).isoformat(),
                'end':             datetime.combine(b.booking_date, b.end_time).isoformat(),
                'backgroundColor': STATUS_COLORS.get(b.status, '#6B7280'),
                'extendedProps': {
                    'booking_number': b.booking_number,
                    'customer_name':  b.customer_name,
                    'event_name':     b.event_name,
                    'event_type':     b.event_type,
                    'property_id':    b.property_id,
                    'property_name':  b.property.name,
                    'slot_type':      b.property_slot.slot_type,
                    'status':         b.status,
                    'payment_status': b.payment_status,
                    'total_amount':   float(b.total_amount),
                },
            })

        return Response(events)


# ---------------------------------------------------------------------------
# Dashboard — scoped per client
# ---------------------------------------------------------------------------

class DashboardView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        today       = timezone.now().date()
        month_start = today.replace(day=1)

        properties = _client_scope(Property.objects.all(), request)
        bookings   = _client_scope(Booking.objects.exclude(status='cancelled'), request)
        payments   = _client_scope(Payment.objects.all(), request)

        total_properties    = properties.count()
        active_properties   = properties.filter(status='active').count()
        total_bookings      = bookings.count()
        today_bookings      = bookings.filter(booking_date=today).count()
        this_month_bookings = bookings.filter(booking_date__gte=month_start).count()

        occupied_slots = bookings.filter(
            booking_date=today,
            status__in=['reserved', 'booked', 'confirmed', 'occupied'],
        ).count()

        total_revenue    = payments.aggregate(total=Sum('amount'))['total'] or 0
        pending_payments = bookings.aggregate(total=Sum('balance_amount'))['total'] or 0

        active_slot_count = PropertySlot.objects.filter(
            status='active', property__in=properties
        ).count()
        occupancy_rate = (
            round((occupied_slots / active_slot_count) * 100, 1) if active_slot_count else 0.0
        )

        recent_bookings = bookings.order_by('-created_at')[:5]
        upcoming_events = bookings.filter(
            booking_date__gte=today,
            status__in=['reserved', 'booked', 'confirmed'],
        ).order_by('booking_date')[:5]

        status_breakdown = dict(
            bookings.values('status').annotate(count=Count('id')).values_list('status', 'count')
        )

        monthly_revenue = []
        for i in range(5, -1, -1):
            m = month_start.month - i
            y = month_start.year
            while m <= 0:
                m += 12
                y -= 1
            revenue = payments.filter(
                payment_date__year=y, payment_date__month=m
            ).aggregate(total=Sum('amount'))['total'] or 0
            monthly_revenue.append({
                'month':   calendar.month_abbr[m],
                'year':    y,
                'revenue': float(revenue),
            })

        data = {
            'total_properties':    total_properties,
            'active_properties':   active_properties,
            'total_bookings':      total_bookings,
            'today_bookings':      today_bookings,
            'this_month_bookings': this_month_bookings,
            'occupied_slots':      occupied_slots,
            'total_revenue':       total_revenue,
            'pending_payments':    pending_payments,
            'occupancy_rate':      occupancy_rate,
            'recent_bookings':     recent_bookings,
            'upcoming_events':     upcoming_events,
            'status_breakdown':    status_breakdown,
            'monthly_revenue':     monthly_revenue,
        }
        return Response(DashboardSerializer(data).data)


# ---------------------------------------------------------------------------
# Reports — scoped per client
# ---------------------------------------------------------------------------

class ReportView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        report_type = request.query_params.get('type', 'revenue')
        date_from   = request.query_params.get('date_from')
        date_to     = request.query_params.get('date_to')

        qs = _client_scope(Booking.objects.exclude(status='cancelled'), request)
        if date_from:
            qs = qs.filter(booking_date__gte=date_from)
        if date_to:
            qs = qs.filter(booking_date__lte=date_to)

        data = {
            'bookings': BookingListSerializer(qs.order_by('-booking_date'), many=True).data,
        }

        if report_type == 'revenue':
            data['total_revenue']   = qs.aggregate(total=Sum('total_amount'))['total'] or 0
            data['total_collected'] = (
                _client_scope(Payment.objects.filter(booking__in=qs), request)
                .aggregate(total=Sum('amount'))['total'] or 0
            )
            data['total_pending'] = qs.aggregate(total=Sum('balance_amount'))['total'] or 0
        elif report_type == 'occupancy':
            data['by_property'] = list(
                qs.values('property__name').annotate(count=Count('id')).order_by('-count')
            )

        return Response(data)


# ---------------------------------------------------------------------------
# Auth — Login / Logout / Me
# ---------------------------------------------------------------------------

class LoginView(APIView):
    """
    POST /api/login/
    Body: { email, client_id, password }

    Flow
    ────
    1. Call the VenueKart activation API and confirm the supplied client_id
       exists, is Active, and its licence has not expired.
    2. Look up the Django user by e-mail address.
    3. Authenticate with Django's password checker.
    4. Verify the user's profile is linked to the same client_id.
    5. Return an auth token plus licence + client metadata.
    """
    permission_classes = [permissions.AllowAny]
    ACTIVATE_API = 'https://activate.imcbs.com/mobileapp/api/project/venuekart/'

    def _fetch_licence_data(self):
        req = urllib.request.Request(
            self.ACTIVATE_API,
            headers={'User-Agent': 'VenueBook/1.0', 'Accept': 'application/json'},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())
        except (urllib.error.URLError, ValueError, OSError):
            return None

    def post(self, request):
        email     = (request.data.get('email')     or '').strip().lower()
        client_id = (request.data.get('client_id') or '').strip().upper()
        password  = (request.data.get('password')  or '')

        if not email or not client_id or not password:
            return Response(
                {'detail': 'Email, Client ID, and password are all required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # ── Step 1: validate client_id via VenueKart activation API ─────────
        api_data = self._fetch_licence_data()
        if api_data is None or not api_data.get('success'):
            return Response(
                {'detail': 'Unable to reach the licence server. Please try again later.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        customers = api_data.get('customers', [])
        matched = next(
            (c for c in customers if (c.get('client_id') or '').upper() == client_id),
            None,
        )

        if not matched:
            return Response(
                {'detail': 'Invalid Client ID – not registered for VenueKart.'},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        if (matched.get('status') or '').lower() != 'active':
            return Response(
                {'detail': 'Your Client ID is inactive. Please contact support.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        validity = matched.get('license_validity', {})
        if validity.get('is_expired', True):
            expiry = validity.get('expiry_date', 'N/A')
            return Response(
                {'detail': f'Your VenueKart licence expired on {expiry}. Please renew to continue.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        # ── Step 2: look up Django user by email ─────────────────────────────
        try:
            user_obj = DjangoUser.objects.get(email__iexact=email)
        except DjangoUser.DoesNotExist:
            return Response(
                {'detail': 'No account found with this email address.'},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        except DjangoUser.MultipleObjectsReturned:
            return Response(
                {'detail': 'Multiple accounts share this email. Please contact support.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # ── Step 3: verify password ───────────────────────────────────────────
        user = authenticate(request, username=user_obj.username, password=password)
        if user is None:
            return Response(
                {'detail': 'Incorrect password. Please try again.'},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        # ── Step 4: resolve profile (create one if missing) ───────────────────
        try:
            profile = user.profile
        except UserProfile.DoesNotExist:
            profile = UserProfile.objects.create(user=user, role='admin', client=None)

        # ── Step 5: ensure a local Client record exists for this client_id ────
        customer_name = matched.get('customer_name', client_id)
        client_obj, _ = Client.objects.update_or_create(
            client_id=client_id,
            defaults={
                'name':      customer_name,
                'is_active': True,
            },
        )

        # Bind the profile to this client so tenant-scoped views work correctly.
        if not profile.is_super_admin:
            profile.client = client_obj
            profile.save(update_fields=['client'])

        # ── Step 6: issue token ───────────────────────────────────────────────
        login(request, user)
        token, _ = Token.objects.get_or_create(user=user)

        return Response({
            'token': token.key,
            'user': {
                'id':           user.id,
                'username':     user.username,
                'email':        user.email,
                'role':         profile.role,
                'is_superuser': user.is_superuser,
            },
            'client': {
                'client_id':      client_id,
                'customer_name':  customer_name,
                'license_expiry': validity.get('expiry_date'),
                'remaining_days': validity.get('remaining_days'),
            },
        })


class LogoutView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        Token.objects.filter(user=request.user).delete()
        logout(request)
        return Response({'detail': 'Logged out.'})


class MeView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = request.user
        try:
            profile     = user.profile
            role        = profile.role
            client_id   = profile.client.client_id if profile.client else None
            client_name = profile.client.name      if profile.client else None
        except UserProfile.DoesNotExist:
            role        = 'super_admin' if user.is_superuser else 'admin'
            client_id   = None
            client_name = None

        return Response({
            'id':           user.id,
            'username':     user.username,
            'email':        user.email,
            'role':         role,
            'is_superuser': user.is_superuser,
            'client_id':    client_id,
            'client_name':  client_name,
        })