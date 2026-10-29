-- Demo metric view on samples.wanderbricks. Run on any SQL warehouse.
-- NOTE: CREATE OR REPLACE drops the view's existing grants. Re-grant consumers afterwards, e.g.
--   GRANT SELECT ON TABLE workspace.genie_demo.booking_metrics TO `<consumer or group>`;
-- (Genie runs SQL as the asker: without SELECT, Genie answers "An internal error occurred".)
CREATE SCHEMA IF NOT EXISTS workspace.genie_demo;

CREATE OR REPLACE VIEW workspace.genie_demo.booking_metrics
WITH METRICS LANGUAGE YAML AS $$
version: 1.1
comment: >-
  Wanderbricks bookings. Time axis is the booking date (created_at). Data ends 2025-07-30.
  PRIVACY BOUNDARY: no personal data by design. The hosts, users, reviews and
  customer_support_logs tables are deliberately not joined, so no names, emails, phones or
  review text can reach Genie. Do not add such columns; the evalops checks
  sql.restricted_columns and report.no_contact_details fail if they appear.
source: samples.wanderbricks.bookings
joins:
  - name: property
    source: samples.wanderbricks.properties
    on: source.property_id = property.property_id
    joins:
      - name: destination
        source: samples.wanderbricks.destinations
        on: property.destination_id = destination.destination_id
        joins:
          - name: country_ref
            source: samples.wanderbricks.countries
            on: destination.country = country_ref.country
dimensions:
  - name: Booking Date
    expr: CAST(source.created_at AS DATE)
  - name: Booking Month
    expr: DATE_TRUNC('MONTH', source.created_at)
  - name: Booking Quarter
    expr: DATE_TRUNC('QUARTER', source.created_at)
  - name: Booking Year
    expr: YEAR(source.created_at)
  - name: Check-in Month
    expr: DATE_TRUNC('MONTH', source.check_in)
  - name: Booking Status
    expr: source.status
  - name: Property Type
    expr: property.property_type
  - name: Destination
    expr: property.destination.destination
  - name: State or Province
    expr: property.destination.state_or_province
  - name: Country
    expr: property.destination.country
  - name: Continent
    expr: property.destination.country_ref.continent
measures:
  - name: Bookings
    expr: COUNT(1)
  - name: Cancelled Bookings
    expr: COUNT(1) FILTER (WHERE source.status = 'cancelled')
  - name: Cancellation Rate
    expr: COUNT(1) FILTER (WHERE source.status = 'cancelled') / COUNT(1)
    comment: A ratio over all bookings in the group. Never average it across groups.
  - name: Gross Booking Value
    expr: SUM(source.total_amount)
    comment: All statuses, including pending and cancelled.
  - name: Realized Revenue
    expr: SUM(source.total_amount) FILTER (WHERE source.status IN ('confirmed', 'completed'))
    comment: '"Revenue" means this measure: confirmed and completed bookings only.'
  - name: Average Booking Value
    expr: SUM(source.total_amount) / COUNT(1)
    comment: Ratio. Never average per-group averages.
  - name: Nights
    expr: SUM(DATEDIFF(source.check_out, source.check_in))
  - name: Average Nightly Rate
    expr: SUM(source.total_amount) / SUM(DATEDIFF(source.check_out, source.check_in))
    comment: Ratio of value to nights. Never average per-booking or per-group rates.
  - name: Guests
    expr: SUM(source.guests_count)
$$;
