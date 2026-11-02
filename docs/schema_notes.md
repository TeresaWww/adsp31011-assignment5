# Schema Notes

Source: `SAKILA.DW` (Snowflake), inventoried with `sql/explore.sql` as read-only role `CLAUDE_RO`.

## Tables

| Table | What one row represents | Row count |
|-------|-------------------------|-----------|
| `FACT_RENTAL` | One rental transaction (`RENTAL_ID` is unique) | 16,044 |
| `AGG_DAILY_STORE_CATEGORY` | One day × store × film category (pre-aggregated revenue and rentals) | 1,231 |
| `DIM_CUSTOMER` | One *version* of a customer (SCD Type 2) | 601 (599 distinct customers) |
| `DIM_FILM` | One *version* of a film (SCD Type 2) | 1,001 (1,000 distinct films) |
| `DIM_DATE` | One calendar day | 731 |
| `DIM_STORE` | One store | 2 |
| `DIM_STAFF` | One staff member | 2 |

## Keys and joins

| Fact column | Joins to |
|-------------|----------|
| `FACT_RENTAL.CUSTOMER_SK` | `DIM_CUSTOMER.CUSTOMER_SK` |
| `FACT_RENTAL.FILM_SK` | `DIM_FILM.FILM_SK` |
| `FACT_RENTAL.RENTAL_DATE_KEY` / `RETURN_DATE_KEY` | `DIM_DATE.DATE_KEY` |
| `FACT_RENTAL.STORE_ID` | `DIM_STORE.STORE_ID` |
| `FACT_RENTAL.STAFF_ID` | `DIM_STAFF.STAFF_ID` |
| `AGG_DAILY_STORE_CATEGORY.DATE_KEY` | `DIM_DATE.DATE_KEY` |
| `AGG_DAILY_STORE_CATEGORY.STORE_ID` | `DIM_STORE.STORE_ID` |

No orphan keys: every fact row matches a customer, film, and date.

## Facts worth knowing

- **Date range of activity:** 2005-05-24 to 2006-02-14. `DIM_DATE` covers 2005-01-01 to 2007-01-01; `DATE_KEY` is an integer `YYYYMMDD`.
- **Activity is sparse:** rentals exist on only 41 days, in roughly one-week bursts every two weeks from 2005-05-24 to 2005-08-23, then nothing until a single day, 2006-02-14 (182 rentals). September 2005 to January 2006 has no data at all, so a monthly trend shows a fake "collapse" in the autumn.
- **Totals:** 16,044 rentals, $67,406.56 revenue (`AMOUNT_PAID`, `NUMBER(8,2)`). 183 rentals are not returned (`IS_RETURNED = FALSE`, `RETURN_DATE_KEY` null).
- **By store:** store 1 (Lethbridge, Canada; manager Mike Hillyer) has 7,923 rentals / $33,679.79; store 2 (Woodridge, Australia; manager Jon Stephens) has 8,121 / $33,726.77.
- `RENTAL_COUNT` and `PAYMENT_COUNT` are always 1, so `COUNT(*)` = `SUM(RENTAL_COUNT)`.
- `AGG_DAILY_STORE_CATEGORY` reconciles exactly with `FACT_RENTAL` (same revenue and rental totals). There are 16 film categories.

## Gotchas (SCD Type 2)

- `DIM_CUSTOMER`: customer_id 1 (MARY SMITH) has 3 versions with different cities. 32 fact rows point to a non-current version.
  - To count customers, use `COUNT(DISTINCT CUSTOMER_ID)`, not row count.
  - Join on `CUSTOMER_SK` for the attributes *at the time of the rental*; join on `CUSTOMER_ID` + `IS_CURRENT` for *current* attributes.
- `DIM_FILM`: film_id 1 (ACADEMY DINOSAUR) has 2 versions; `RENTAL_RATE` changed from 0.99 to 5.99.
  - To list films, filter `IS_CURRENT = TRUE` or use `COUNT(DISTINCT FILM_ID)`.
- `EFFECTIVE_FROM` of `1900-01-01` and `EFFECTIVE_TO` of `9999-12-31` are sentinel values, not real dates.
