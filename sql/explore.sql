-- Step 1: data inventory
-- Run in a Snowsight worksheet after selecting your warehouse / database / schema.

-- 1. Current context
SELECT CURRENT_WAREHOUSE(), CURRENT_DATABASE(), CURRENT_SCHEMA();

-- 2. All tables in the current schema with row counts
SELECT table_name, table_type, row_count
FROM information_schema.tables
WHERE table_schema = CURRENT_SCHEMA()
ORDER BY table_name;

-- 3. Columns of every table (to decide what one row represents)
SELECT table_name, column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_schema = CURRENT_SCHEMA()
ORDER BY table_name, ordinal_position;

-- 4. Peek at sample rows (repeat for each table)
-- SELECT * FROM <table> LIMIT 10;

-- 5. Check the grain: is this column unique per row? (repeat for each table)
-- SELECT COUNT(*) AS total_rows, COUNT(DISTINCT <candidate_key>) AS distinct_keys
-- FROM <table>;

-- 6. Grain and SCD2 checks used for docs/schema_notes.md
SELECT COUNT(*), COUNT(DISTINCT rental_id), MIN(rental_ts), MAX(rental_ts), SUM(amount_paid)
FROM fact_rental;

SELECT COUNT(*), COUNT(DISTINCT customer_id), SUM(IFF(is_current, 1, 0)) FROM dim_customer;
SELECT COUNT(*), COUNT(DISTINCT film_id), SUM(IFF(is_current, 1, 0)) FROM dim_film;

SELECT COUNT(*), COUNT(DISTINCT date_key, store_id, category), SUM(revenue), SUM(rentals)
FROM agg_daily_store_category;

-- Orphan keys in the fact table
SELECT SUM(IFF(c.customer_sk IS NULL, 1, 0)) AS missing_customer,
       SUM(IFF(f2.film_sk IS NULL, 1, 0))    AS missing_film,
       SUM(IFF(d.date_key IS NULL, 1, 0))    AS missing_date
FROM fact_rental f
LEFT JOIN dim_customer c  ON c.customer_sk = f.customer_sk
LEFT JOIN dim_film f2     ON f2.film_sk = f.film_sk
LEFT JOIN dim_date d      ON d.date_key = f.rental_date_key;
