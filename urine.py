import pandas as pd
import math

def rate_to_hourly_urine(input_csv, output_csv, max_fill_hours=None):
    """Convert HiRID 10020000 rates (mL/h) into hourly amounts (mL).

    Input columns: subject_id, charttime, value. If itemid/variableid is
    present, only 10020000 is used. Each reading applies on [its timestamp,
    the next reading), optionally capped at max_fill_hours. The last reading
    is not extrapolated because its end time is unknown.

    Integrate actual overlaps with clock hours, then sum within each hour.
    Output boundaries are whole hours, but a partially observed hour contains
    only its observed amount, with no scaling to a full hour. Entirely missing
    hours are omitted; measured zero rates produce zero amounts.
    Conflicting readings at the same subject/time raise an error.
    Writes a separate CSV and returns the resulting DataFrame.
    """
    if input_csv == output_csv:
        raise ValueError("Input and output must be different files.")
    hour_ns = 3_600_000_000_000
    cap_ns = None
    if max_fill_hours is not None:
        hours = float(max_fill_hours)
        if not math.isfinite(hours) or hours <= 0:
            raise ValueError("max_fill_hours must be positive and finite, or None.")
        cap_ns = int(pd.Timedelta(hours=hours).value)
        if cap_ns <= 0:
            raise ValueError("max_fill_hours is too small.")

    df = pd.read_csv(input_csv)

    df = df[["subject_id", "charttime", "value"]].copy()
    df["charttime"] = pd.to_datetime(df["charttime"], errors="raise")
    df["value"] = pd.to_numeric(df["value"], errors="raise")
    if df.isna().any().any():
        raise ValueError("subject_id, charttime and value cannot be missing.")
    if not df["value"].map(math.isfinite).all() or df["value"].lt(0).any():
        raise ValueError("Urine rates must be finite and nonnegative.")
    df = df.drop_duplicates(["subject_id", "charttime", "value"])
    if df.duplicated(["subject_id", "charttime"]).any():
        raise ValueError("Conflicting urine rates for the same subject_id/charttime.")
    df = df.sort_values(["subject_id", "charttime"], kind="stable")

    rows = []
    for subject_id, group in df.groupby("subject_id", sort=False):
        times = [timestamp.value for timestamp in group["charttime"]]
        rates = group["value"].to_numpy(dtype=float)
        amounts = {}
        for index in range(len(times) - 1):
            start = times[index]
            end = times[index + 1]
            if cap_ns is not None:
                end = min(end, start + cap_ns)
            while start < end:
                hour_start = (start // hour_ns) * hour_ns
                overlap_end = min(end, hour_start + hour_ns)
                volume = rates[index] * ((overlap_end - start) / hour_ns)
                amounts[hour_start] = amounts.get(hour_start, 0.0) + volume
                start = overlap_end
        for start, amount in sorted(amounts.items()):
            rows.append((subject_id, pd.Timestamp(start),
                         pd.Timestamp(start + hour_ns), amount))

    result = pd.DataFrame(rows, columns=["subject_id", "starttime", "endtime", "value"])
    result.to_csv(output_csv, index=False)
    return result

def filter_large_csv_by_stay_ids(input_file, output_file, key_col, chunksize=100_000):
    valid_ids = [10020000]

    reader = pd.read_csv(input_file, chunksize=chunksize)
    first_chunk = True

    total_rows_kept = 0
    for i, chunk in enumerate(reader):
        filtered_chunk = chunk[chunk[key_col].isin(valid_ids)]
        total_rows_kept += len(filtered_chunk)

        if first_chunk:
            filtered_chunk.to_csv(output_file, index=False, mode='w')
            first_chunk = False
        else:
            filtered_chunk.to_csv(output_file, index=False, mode='a', header=False)

        print(f"Chunk {i+1} done. {len(filtered_chunk)} rows kept.")

def merge_rename_filter(input_csv, output_csv):

    df = pd.read_csv(input_csv)

    df = df[["observation_tables/", "patientid", "value"]]
    df.rename(columns={'patientid': 'subject_id'}, inplace=True)
    df.rename(columns={'observation_tables/': 'charttime'}, inplace=True)

    df = df.drop_duplicates(keep='first')
    df = df.loc[df["value"].ge(0)].copy()

    df.sort_values(by=['subject_id', 'charttime'], inplace=True)

    df.to_csv(output_csv, index=False)
    print(f"Done, result save to: {output_csv}")

    return df

def add_hourly_urine_totals(input_csv, output_csv):
    df = pd.read_csv(
        input_csv,
        parse_dates=["starttime", "endtime"],
    )
    df = df.sort_values(
        ["subject_id", "starttime"]
    ).reset_index(drop=True)

    if df[["subject_id", "starttime", "endtime"]].isna().any().any():
        raise ValueError("患者ID和起止时间不能缺失。")

    if df.duplicated(["subject_id", "starttime"]).any():
        raise ValueError("同一患者存在重复小时记录。")

    if not (
        df["endtime"] - df["starttime"]
    ).eq(pd.Timedelta(hours=1)).all():
        raise ValueError("每条记录必须对应一个小时。")

    df["value"] = pd.to_numeric(df["value"], errors="raise")

    first_start = df.groupby("subject_id")["starttime"].transform("min")
    df["hour"] = (
        (df["starttime"] - first_start) / pd.Timedelta(hours=1)
    ).astype("int64") + 1

    groups = df.groupby("subject_id", sort=False)

    for hours in (6, 8, 12, 24):
        totals = (
            groups["value"]
            .rolling(window=hours, min_periods=hours)
            .sum()
            .reset_index(level=0, drop=True)
        )

        first_hour_in_window = groups["hour"].shift(hours - 1)
        continuous = (
            df["hour"] - first_hour_in_window
        ).eq(hours - 1)

        df[f"urine_{hours}h"] = totals.where(continuous)

    df.to_csv(output_csv, index=False)

    return df

if __name__ == "__main__":

    filter_large_csv_by_stay_ids('../../hirid/observation_tables.csv', './urine.csv', 'variableid')
    merge_rename_filter('urine.csv', 'urine.csv')
    rate_to_hourly_urine('urine.csv', 'hourly_urine.csv', max_fill_hours=None)
    add_hourly_urine_totals('hourly_urine.csv', 'hourly_urine.csv')
