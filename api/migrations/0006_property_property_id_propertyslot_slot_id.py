"""
Custom migration: add property_id to Property and slot_id to PropertySlot.

Why not use the auto-generated migration?
──────────────────────────────────────────
The auto-generated file adds the column with unique=True in a single step.
Postgres tries to build the unique index immediately, but every existing row
gets the blank default '' — duplicate across all rows → IntegrityError.

This migration does it safely in three steps:
  1. Add the column WITHOUT the unique constraint (nullable temporarily).
  2. RunPython: call each model's _generate_*_id() logic to backfill every
     existing row with a proper unique value.
  3. ALTER the column to NOT NULL + add the unique index.
"""

import re
from django.db import migrations, models


# ---------------------------------------------------------------------------
# Helpers — must be self-contained (no imports from app code)
# ---------------------------------------------------------------------------

PROPERTY_TYPE_CODES = {
    'auditorium': 'AUD',
    'house':      'HSE',
    'villa':      'VIL',
    'resort':     'RST',
    'plot':       'PLT',
    'commercial': 'COM',
}

SLOT_TYPE_CODES = {
    'full_day': 'FD',
    'half_day': 'HD',
    'hourly':   'HR',
}


def _slugify(name, max_chars=8):
    cleaned = re.sub(r'[^A-Za-z0-9]', '', name)
    return cleaned.upper()[:max_chars]


def backfill_property_ids(apps, schema_editor):
    Property = apps.get_model('api', 'Property')

    # Group existing rows by (client_id_str, type_code) so we can assign
    # sequential numbers within each group.
    # We iterate ordered by pk so the sequence is stable and reproducible.
    counters = {}   # (client_part, type_code) → next_seq int

    for prop in Property.objects.select_related('client').order_by('id'):
        client_part = prop.client.client_id if prop.client_id else 'GEN'
        type_code   = PROPERTY_TYPE_CODES.get(prop.property_type,
                                               prop.property_type[:3].upper())
        key = (client_part, type_code)
        counters[key] = counters.get(key, 0) + 1
        prop.property_id = f"{client_part}-{type_code}-{counters[key]:03d}"
        prop.save(update_fields=['property_id'])


def backfill_slot_ids(apps, schema_editor):
    PropertySlot = apps.get_model('api', 'PropertySlot')

    counters = {}   # (client_part, prop_slug, slot_code) → next_seq int

    for slot in PropertySlot.objects.select_related('property__client').order_by('id'):
        prop        = slot.property
        client_part = prop.client.client_id if prop.client_id else 'GEN'
        prop_slug   = _slugify(prop.name, max_chars=8)
        slot_code   = SLOT_TYPE_CODES.get(slot.slot_type,
                                           slot.slot_type[:2].upper())
        key = (client_part, prop_slug, slot_code)
        counters[key] = counters.get(key, 0) + 1
        slot.slot_id = f"{client_part}-{prop_slug}-{slot_code}-{counters[key]:02d}"
        slot.save(update_fields=['slot_id'])


def reverse_backfill(apps, schema_editor):
    # Nothing to undo — the columns themselves are removed by the field
    # operations in the reverse migration list.
    pass


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

class Migration(migrations.Migration):

    dependencies = [
        ('api', '0005_client_booking_client_payment_client_property_client_and_more'),   # ← replace with your actual last migration
    ]

    operations = [
        # ── Step 1: add columns WITHOUT unique constraint ─────────────────────
        migrations.AddField(
            model_name='property',
            name='property_id',
            field=models.CharField(
                max_length=30,
                blank=True,
                default='',      # temporary default so existing rows get ''
                editable=False,
            ),
        ),
        migrations.AddField(
            model_name='propertyslot',
            name='slot_id',
            field=models.CharField(
                max_length=40,
                blank=True,
                default='',
                editable=False,
            ),
        ),

        # ── Step 2: backfill every existing row with a real unique value ──────
        migrations.RunPython(backfill_property_ids, reverse_code=reverse_backfill),
        migrations.RunPython(backfill_slot_ids,     reverse_code=reverse_backfill),

        # ── Step 3: tighten the columns — unique + remove the temp default ────
        migrations.AlterField(
            model_name='property',
            name='property_id',
            field=models.CharField(
                max_length=30,
                unique=True,
                blank=True,
                editable=False,
            ),
        ),
        migrations.AlterField(
            model_name='propertyslot',
            name='slot_id',
            field=models.CharField(
                max_length=40,
                unique=True,
                blank=True,
                editable=False,
            ),
        ),
    ]
