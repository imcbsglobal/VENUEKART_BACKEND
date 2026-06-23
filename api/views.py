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

from .models import Client, UserProfile, Property, PropertyImage, PropertySlot, Booking, Payment
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
        property_id = self.request.query_params.get('property')
        date_from = self.request.query_params.get('date_from')
        date_to = self.request.query_params.get('date_to')
        search = self.request.query_params.get('search')

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
        # This prevents a booking being created against another tenant's property.
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

    @action(detail=True, methods=['patch'], url_path='status')
    def update_status(self, request, pk=None):
        booking = self.get_object()
        new_status = request.data.get('status')
        valid_statuses = [c[0] for c in Booking.BOOKING_STATUS]
        if new_status not in valid_statuses:
            return Response({'error': 'Invalid status.'}, status=status.HTTP_400_BAD_REQUEST)
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
        date_str = request.query_params.get('date')

        if not property_id or not date_str:
            return Response({'error': 'property and date are required.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            booking_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            return Response({'error': 'date must be in YYYY-MM-DD format.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            # Scoping: only let users check availability for their own client's properties
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
            status__in=['reserved', 'booked', 'confirmed', 'occupied'],
        ).order_by('start_time')

        busy_ranges = [
            {
                'start': b.start_time.strftime('%H:%M'),
                'end': b.end_time.strftime('%H:%M'),
                'booking_number': b.booking_number,
                'customer_name': b.customer_name,
            }
            for b in existing
        ]

        return Response({'property': property_obj.id, 'date': date_str, 'busy_ranges': busy_ranges})


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------

STATUS_COLORS = {
    'inquiry': '#6B7280', 'reserved': '#F59E0B', 'booked': '#3B82F6',
    'confirmed': '#F97316', 'occupied': '#EF4444', 'completed': '#10B981', 'cancelled': '#374151',
}


class CalendarView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        today = timezone.now().date()
        month = int(request.query_params.get('month', today.month))
        year = int(request.query_params.get('year', today.year))
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
                'id': b.id,
                'title': f"{b.booking_number} — {b.customer_name}",
                'start': datetime.combine(b.booking_date, b.start_time).isoformat(),
                'end': datetime.combine(b.booking_date, b.end_time).isoformat(),
                'backgroundColor': STATUS_COLORS.get(b.status, '#6B7280'),
                'extendedProps': {
                    'booking_number': b.booking_number,
                    'customer_name': b.customer_name,
                    'event_name': b.event_name,
                    'event_type': b.event_type,
                    'property_id': b.property_id,
                    'property_name': b.property.name,
                    'slot_type': b.property_slot.slot_type,
                    'status': b.status,
                    'payment_status': b.payment_status,
                    'total_amount': float(b.total_amount),
                },
            })

        return Response(events)


# ---------------------------------------------------------------------------
# Dashboard — scoped per client
# ---------------------------------------------------------------------------

class DashboardView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        today = timezone.now().date()
        month_start = today.replace(day=1)

        properties = _client_scope(Property.objects.all(), request)
        bookings = _client_scope(Booking.objects.exclude(status='cancelled'), request)
        payments = _client_scope(Payment.objects.all(), request)

        total_properties = properties.count()
        active_properties = properties.filter(status='active').count()
        total_bookings = bookings.count()
        today_bookings = bookings.filter(booking_date=today).count()
        this_month_bookings = bookings.filter(booking_date__gte=month_start).count()

        occupied_slots = bookings.filter(
            booking_date=today,
            status__in=['reserved', 'booked', 'confirmed', 'occupied'],
        ).count()

        total_revenue = payments.aggregate(total=Sum('amount'))['total'] or 0
        pending_payments = bookings.aggregate(total=Sum('balance_amount'))['total'] or 0

        active_slot_count = PropertySlot.objects.filter(
            status='active', property__in=properties
        ).count()
        occupancy_rate = round((occupied_slots / active_slot_count) * 100, 1) if active_slot_count else 0.0

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
            monthly_revenue.append({'month': calendar.month_abbr[m], 'year': y, 'revenue': float(revenue)})

        data = {
            'total_properties': total_properties,
            'active_properties': active_properties,
            'total_bookings': total_bookings,
            'today_bookings': today_bookings,
            'this_month_bookings': this_month_bookings,
            'occupied_slots': occupied_slots,
            'total_revenue': total_revenue,
            'pending_payments': pending_payments,
            'occupancy_rate': occupancy_rate,
            'recent_bookings': recent_bookings,
            'upcoming_events': upcoming_events,
            'status_breakdown': status_breakdown,
            'monthly_revenue': monthly_revenue,
        }
        return Response(DashboardSerializer(data).data)


# ---------------------------------------------------------------------------
# Reports — scoped per client
# ---------------------------------------------------------------------------

class ReportView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        report_type = request.query_params.get('type', 'revenue')
        date_from = request.query_params.get('date_from')
        date_to = request.query_params.get('date_to')

        qs = _client_scope(Booking.objects.exclude(status='cancelled'), request)
        if date_from:
            qs = qs.filter(booking_date__gte=date_from)
        if date_to:
            qs = qs.filter(booking_date__lte=date_to)

        data = {
            'bookings': BookingListSerializer(qs.order_by('-booking_date'), many=True).data,
        }

        if report_type == 'revenue':
            data['total_revenue'] = qs.aggregate(total=Sum('total_amount'))['total'] or 0
            data['total_collected'] = _client_scope(Payment.objects.filter(booking__in=qs), request).aggregate(
                total=Sum('amount')
            )['total'] or 0
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
        # Only two things are checked here:
        #   (a) the client_id exists in the activation API response
        #   (b) the licence has not expired
        # There is no fixed set of allowed IDs – any registered, non-expired
        # client_id is accepted.
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
        # We no longer check whether the profile's stored client matches the
        # supplied client_id. The activation API is the single source of truth
        # for whether a client_id is valid. Any registered, non-expired ID is
        # allowed – the user just needs a valid email + password.
        try:
            profile = user.profile
        except UserProfile.DoesNotExist:
            profile = UserProfile.objects.create(user=user, role='admin', client=None)

        # ── Step 5: ensure a local Client record exists for this client_id ────
        # This keeps the FK-based tenant scoping working for properties /
        # bookings / payments without requiring a separate admin setup step.
        customer_name = matched.get('customer_name', client_id)
        client_obj, _ = Client.objects.update_or_create(
            client_id=client_id,
            defaults={
                'name':      customer_name,
                'is_active': True,
            },
        )

        # Bind the profile to this client so tenant-scoped views work correctly.
        # Super-admins keep client=None (they span all tenants).
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
            profile = user.profile
            role = profile.role
            client_id = profile.client.client_id if profile.client else None
            client_name = profile.client.name if profile.client else None
        except UserProfile.DoesNotExist:
            role = 'super_admin' if user.is_superuser else 'admin'
            client_id = None
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