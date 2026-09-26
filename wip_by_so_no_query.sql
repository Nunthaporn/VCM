-- WIP pieces and barcode count: use CUT as the canonical base for every process.
-- WIP condition: source process has a scan time and the next process has no scan time.
WITH process_events AS (
    SELECT
        plant,
        so_year,
        so_no,
        barcode_no AS barcode,
        'CUT' AS process,
        MIN(
            CASE
                WHEN (cut_date IS NULL OR cut_date::time = TIME '00:00:00')
                     AND upd_date IS NOT NULL
                    THEN upd_date
                ELSE cut_date
            END
        ) AS process_time,
        COALESCE(SUM(cut_qty), 0) AS process_qty,
        COUNT(barcode_no) AS process_barcode_count
    FROM public.v_cut
    WHERE barcode_no IS NOT NULL
    GROUP BY plant, so_year, so_no, barcode_no

    UNION ALL

    SELECT plant, so_year, so_no, barcode, 'SM2', MIN(ww_in_date), COALESCE(SUM(ww_in_qty), 0), COUNT(barcode)
    FROM public.v_sm2
    WHERE barcode IS NOT NULL
      AND ww_in_date >= DATE '2026-07-09'
    GROUP BY plant, so_year, so_no, barcode

    UNION ALL

    SELECT plant, so_year, so_no, barcode, 'SM3', MIN(ww_in_date), COALESCE(SUM(ww_in_qty), 0), COUNT(barcode)
    FROM public.v_sm3
    WHERE barcode IS NOT NULL
    GROUP BY plant, so_year, so_no, barcode

    UNION ALL

    SELECT plant, so_year, so_no, barcode_no, 'SEW', MIN(loading_date), COALESCE(SUM(qty), 0), COUNT(barcode_no)
    FROM public.v_sew
    WHERE barcode_no IS NOT NULL
    GROUP BY plant, so_year, so_no, barcode_no

    UNION ALL

    SELECT plant, so_year, so_no, barcode_no, 'WAIT_FN', MIN(wait_fn_date), COALESCE(SUM(wait_fn_qty), 0), COUNT(barcode_no)
    FROM public.v_sew
    WHERE barcode_no IS NOT NULL
      AND wait_fn_date >= DATE '2026-07-09'
    GROUP BY plant, so_year, so_no, barcode_no

    UNION ALL

    SELECT plant, so_year, so_no, barcode_no, 'PACK', MIN(entry_date), COALESCE(SUM(qty), 0), COUNT(barcode_no)
    FROM public.v_pack
    WHERE barcode_no IS NOT NULL
    GROUP BY plant, so_year, so_no, barcode_no
),
barcode_times AS (
    SELECT
        plant,
        so_year,
        so_no,
        barcode,
        MIN(process_time) FILTER (WHERE process = 'CUT') AS cut_time,
        MIN(process_time) FILTER (WHERE process = 'SM2') AS sm2_time,
        MIN(process_time) FILTER (WHERE process = 'SM3') AS sm3_time,
        MIN(process_time) FILTER (WHERE process = 'SEW') AS sew_time,
        MIN(process_time) FILTER (WHERE process = 'WAIT_FN') AS wait_fn_time,
        MIN(process_time) FILTER (WHERE process = 'PACK') AS pack_time,
        COALESCE(SUM(process_qty) FILTER (WHERE process = 'CUT'), 0) AS cut_qty,
        COALESCE(SUM(process_qty) FILTER (WHERE process = 'SM2'), 0) AS sm2_qty,
        COALESCE(SUM(process_qty) FILTER (WHERE process = 'SM3'), 0) AS sm3_qty,
        COALESCE(SUM(process_qty) FILTER (WHERE process = 'SEW'), 0) AS sew_qty,
        COALESCE(SUM(process_qty) FILTER (WHERE process = 'WAIT_FN'), 0) AS wait_fn_qty,
        COALESCE(SUM(process_qty) FILTER (WHERE process = 'PACK'), 0) AS pack_qty,
        COALESCE(SUM(process_barcode_count) FILTER (WHERE process = 'CUT'), 0) AS cut_barcode_count,
        COALESCE(SUM(process_barcode_count) FILTER (WHERE process = 'SM2'), 0) AS sm2_barcode_count,
        COALESCE(SUM(process_barcode_count) FILTER (WHERE process = 'SM3'), 0) AS sm3_barcode_count,
        COALESCE(SUM(process_barcode_count) FILTER (WHERE process = 'SEW'), 0) AS sew_barcode_count,
        COALESCE(SUM(process_barcode_count) FILTER (WHERE process = 'WAIT_FN'), 0) AS wait_fn_barcode_count,
        COALESCE(SUM(process_barcode_count) FILTER (WHERE process = 'PACK'), 0) AS pack_barcode_count
    FROM process_events
    GROUP BY plant, so_year, so_no, barcode
),
wip_by_so AS (
SELECT
    plant,
    so_year,
    so_no,
    COALESCE(SUM(cut_qty) FILTER (
        WHERE cut_time IS NOT NULL AND sm2_time IS NULL
    ), 0) AS cut_wip_pcs,
    COALESCE(SUM(cut_barcode_count) FILTER (
        WHERE cut_time IS NOT NULL AND sm2_time IS NULL
    ), 0) AS cut_wip_barcode_count,
    COALESCE(SUM(cut_qty) FILTER (
        WHERE sm2_time IS NOT NULL AND sm3_time IS NULL
    ), 0) AS sm2_wip_pcs,
    COALESCE(SUM(cut_barcode_count) FILTER (
        WHERE sm2_time IS NOT NULL AND sm3_time IS NULL
    ), 0) AS sm2_wip_barcode_count,
    COALESCE(SUM(cut_qty) FILTER (
        WHERE sm3_time IS NOT NULL AND sew_time IS NULL
    ), 0) AS sm3_wip_pcs,
    COALESCE(SUM(cut_barcode_count) FILTER (
        WHERE sm3_time IS NOT NULL AND sew_time IS NULL
    ), 0) AS sm3_wip_barcode_count,
    COALESCE(SUM(cut_qty) FILTER (
        WHERE sew_time IS NOT NULL AND wait_fn_time IS NULL
    ), 0) AS sew_wip_pcs,
    COALESCE(SUM(cut_barcode_count) FILTER (
        WHERE sew_time IS NOT NULL AND wait_fn_time IS NULL
    ), 0) AS sew_wip_barcode_count,
    COALESCE(SUM(cut_qty) FILTER (
        WHERE wait_fn_time IS NOT NULL AND pack_time IS NULL
    ), 0) AS wait_fn_wip_pcs,
    COALESCE(SUM(cut_barcode_count) FILTER (
        WHERE wait_fn_time IS NOT NULL AND pack_time IS NULL
    ), 0) AS wait_fn_wip_barcode_count,
    0 AS pack_wip_pcs,
    0 AS pack_wip_barcode_count
FROM barcode_times
GROUP BY plant, so_year, so_no
)
SELECT
    *,
    cut_wip_pcs
      + sm2_wip_pcs
      + sm3_wip_pcs
      + sew_wip_pcs
      + wait_fn_wip_pcs AS total_wip_pcs,
    cut_wip_barcode_count
      + sm2_wip_barcode_count
      + sm3_wip_barcode_count
      + sew_wip_barcode_count
      + wait_fn_wip_barcode_count AS total_wip_barcode_count
FROM wip_by_so
ORDER BY plant, so_year, so_no;
