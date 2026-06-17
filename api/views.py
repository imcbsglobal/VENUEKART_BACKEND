import calendar
from datetime import datetime, timedelta

from django.contrib.auth import authenticate, login, logout
from django.db.models import Sum, Count, Q
from django.utils import timezone

from rest_framework import viewsets, status, permissions
from rest_framework.authtoken.models import Token
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Property, PropertyImage, PropertySlot, Booking, Payment
from .serializers import (
    PropertySerializer,
    PropertyImageSerializer,
    PropertySlotSerializer,
    BookingListSerializer,
    BookingDetailSerializer,
    PaymentSerializer,
    DashboardSerializer,
)


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

class PropertyViewSet(viewsets.ModelViewSet):
    queryset = Property.objects.all().prefetch_related('images', 'slots')
    serializer_class = PropertySerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = super().get_queryset()
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

    @action(detail=True, methods=['get'])
    def slots(self, request, pk=None):
        """
        GET /api/properties/<id>/slots/
        Used by the booking form: lists only the slot types this property
        actually offers (the ones ticked on the property form), instead of
        the old global Slot list.
        """
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
        qs = super().get_queryset()
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
# Availability check — given a property + date, says which of its slot
# types are free. For hourly slots it also returns the open sub-windows
# left on that date, since hourly bookings are a custom start+duration now.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Availability check — given a property + date, lists the bookings already
# on the books so the booking form can show what times are taken. Actual
# conflict prevention happens in BookingDetailSerializer.validate() / 
# Booking.clean() when the booking is submitted.
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
            property_obj = Property.objects.get(pk=property_id)
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

        return Response({
            'property': property_obj.id,
            'date': date_str,
            'busy_ranges': busy_ranges,
        })


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------

# Mirrors the STATUS_COLORS map in AdminDashboard.jsx so calendar event
# colors match the rest of the UI.
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

        qs = Booking.objects.filter(
            booking_date__year=year,
            booking_date__month=month,
        ).exclude(status='cancelled').select_related('property', 'property_slot')

        if property_id:
            qs = qs.filter(property_id=property_id)

        # The frontend's CalendarPage expects FullCalendar-style event
        # objects (start/end/title/backgroundColor/extendedProps), not flat
        # booking rows — it does `new Date(ev.start)` and reads
        # `ev.extendedProps.customer_name` directly.
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
# Dashboard
# ---------------------------------------------------------------------------

class DashboardView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        today = timezone.now().date()
        month_start = today.replace(day=1)

        properties = Property.objects.all()
        bookings = Booking.objects.exclude(status='cancelled')

        total_properties = properties.count()
        active_properties = properties.filter(status='active').count()
        total_bookings = bookings.count()
        today_bookings = bookings.filter(booking_date=today).count()
        this_month_bookings = bookings.filter(booking_date__gte=month_start).count()

        occupied_slots = bookings.filter(
            booking_date=today,
            status__in=['reserved', 'booked', 'confirmed', 'occupied'],
        ).count()

        total_revenue = Payment.objects.aggregate(total=Sum('amount'))['total'] or 0
        pending_payments = bookings.aggregate(total=Sum('balance_amount'))['total'] or 0

        active_slot_count = PropertySlot.objects.filter(status='active').count()
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
            revenue = Payment.objects.filter(
                payment_date__year=y, payment_date__month=m
            ).aggregate(total=Sum('amount'))['total'] or 0
            monthly_revenue.append({
                'month': calendar.month_abbr[m],
                'year': y,
                'revenue': float(revenue),
            })

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
# Reports — matches AdminDashboard.jsx's ReportsPage, which sends
# ?type=revenue|occupancy|booking&date_from=&date_to= and reads
# total_revenue/total_collected/total_pending/bookings for 'revenue',
# by_property (property__name, count) for 'occupancy', and bookings for both.
# ---------------------------------------------------------------------------

class ReportView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        report_type = request.query_params.get('type', 'revenue')
        date_from = request.query_params.get('date_from')
        date_to = request.query_params.get('date_to')

        qs = Booking.objects.exclude(status='cancelled')
        if date_from:
            qs = qs.filter(booking_date__gte=date_from)
        if date_to:
            qs = qs.filter(booking_date__lte=date_to)

        data = {
            'bookings': BookingListSerializer(qs.order_by('-booking_date'), many=True).data,
        }

        if report_type == 'revenue':
            data['total_revenue'] = qs.aggregate(total=Sum('total_amount'))['total'] or 0
            data['total_collected'] = Payment.objects.filter(booking__in=qs).aggregate(
                total=Sum('amount')
            )['total'] or 0
            data['total_pending'] = qs.aggregate(total=Sum('balance_amount'))['total'] or 0
        elif report_type == 'occupancy':
            data['by_property'] = list(
                qs.values('property__name').annotate(count=Count('id')).order_by('-count')
            )

        return Response(data)


# ---------------------------------------------------------------------------
# Auth — GUESS: implemented with DRF Token auth since that's the most common
# pattern for this kind of API, but I have no idea what your original
# Login/Logout/Me actually did (session auth? JWT? something custom?). If
# your frontend already has working login/logout calls, keep your original
# versions of these three and only take the views above.
# ---------------------------------------------------------------------------

class LoginView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        username = request.data.get('username')
        password = request.data.get('password')
        user = authenticate(request, username=username, password=password)

        if user is None:
            return Response({'error': 'Invalid credentials.'}, status=status.HTTP_401_UNAUTHORIZED)

        login(request, user)
        token, _ = Token.objects.get_or_create(user=user)
        return Response({
            'token': token.key,
            'username': user.username,
            'is_superuser': user.is_superuser,
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
        return Response({
            'username': user.username,
            'email': user.email,
            'is_superuser': user.is_superuser,
        })