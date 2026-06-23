from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    ClientViewSet,
    UserViewSet,
    PropertyViewSet,
    BookingViewSet,
    EnquiryViewSet,
    PaymentViewSet,
    CalendarView,
    DashboardView,
    AvailabilityCheckView,
    ReportView,
    LoginView,
    LogoutView,
    MeView,
)

router = DefaultRouter()

# ── Super-admin management ────────────────────────────────────────────────────
router.register('clients', ClientViewSet, basename='client')   # tenant CRUD
router.register('users',   UserViewSet,   basename='user')     # user CRUD

# ── Core resources (tenant-scoped by the views) ───────────────────────────────
router.register('properties', PropertyViewSet, basename='property')
router.register('enquiries',  EnquiryViewSet,  basename='enquiry')
router.register('bookings',   BookingViewSet,  basename='booking')
router.register('payments',   PaymentViewSet,  basename='payment')

urlpatterns = [
    path('', include(router.urls)),
    path('calendar/',           CalendarView.as_view(),       name='calendar'),
    path('dashboard/',          DashboardView.as_view(),      name='dashboard'),
    path('availability-check/', AvailabilityCheckView.as_view(), name='availability-check'),
    path('reports/',            ReportView.as_view(),         name='reports'),
    path('login/',              LoginView.as_view(),          name='auth-login'),
    path('logout/',             LogoutView.as_view(),         name='auth-logout'),
    path('me/',                 MeView.as_view(),             name='auth-me'),
]