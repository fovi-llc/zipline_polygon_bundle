from zipline.data.bundles import register

from config import PolygonConfig
from concat_all_aggs import concat_all_aggs_from_csv, generate_csv_agg_tables
from adjustments import load_splits, load_dividends

import pyarrow
import pyarrow.compute

import pandas as pd
import os
import logging


# TODO: Change warnings to be relative to number of days in the range.


def symbol_to_upper(s: str) -> str:
    if s.isupper():
        return s
    return "".join(map(lambda c: ("^" + c.upper()) if c.islower() else c, s))


def generate_all_agg_tables_from_csv(
    config: PolygonConfig,
):
    paths, schema, tables = generate_csv_agg_tables(config)
    for table in tables:
        table = table.sort_by([("ticker", "ascending"), ("window_start", "ascending")])
        yield table


# def remove_duplicated_index(df: pd.DataFrame) -> pd.DataFrame:
#     duplicated_index = df.index.duplicated(keep=False)
#     if not duplicated_index.any():
#         return df
#     # Find duplicate index values (date) with zero volume or transactions
#     duplicated_index_with_zero_activity = duplicated_index & (
#         df["volume"] == 0) | (df["transactions"] == 0)
#     if duplicated_index_with_zero_activity.any():
#         print(
#             f" WARNING: Got dupes with zero activity {df[duplicated_index_with_zero_activity]=}"
#         )
#         df = df[~duplicated_index_with_zero_activity]
#         duplicated_index = df.index.duplicated(keep=False)
#         if not duplicated_index.any():
#             return df
#     print(f" WARNING: Dropping dupes {df[duplicated_index]=}")
#     df = df[df.index.duplicated(keep="first")]
#     return df


def aggregate_multiple_aggs_per_date(df: pd.DataFrame) -> pd.DataFrame:
    duplicated_index = df.index.duplicated(keep=False)
    if not duplicated_index.any():
        return df
    duplicates = df[duplicated_index]
    duplicate_index_values = duplicates.index.values
    print()
    if duplicates["symbol"].nunique() != 1:
        logging.error(f"{duplicates['symbol'].unique()=} {duplicate_index_values=}")
    logging.warning(f"Aggregating dupes df[df.index.duplicated(keep=False)]=\n{duplicates}")
    df = df.groupby(df.index).agg(
        {
            "symbol": "first",
            "volume": "sum",
            "open": "first",
            "close": "last",
            "high": "max",
            "low": "min",
            "transactions": "sum",
        }
    )
    print(
        f"WARNING: Aggregated dupes df=\n{df[df.index.isin(duplicate_index_values)]}"
    )
    return df


def process_day_aggregates(
    table,
    sessions,
    metadata,
    calendar,
    symbol_to_sid: dict[str, int],
    dates_with_data: set,
):
    for symbol, sid in symbol_to_sid.items():
        df = table.filter(
            pyarrow.compute.field("symbol") == pyarrow.scalar(symbol)
        ).to_pandas()
        # The SQL schema zipline uses for symbols ignores case
        symbol_escaped = symbol_to_upper(symbol)
        df["symbol"] = symbol_escaped
        df["day"] = pd.to_datetime(df["day"].dt.date)
        df = df.set_index("day")
        if not df.index.is_monotonic_increasing:
            print(f" INFO: {symbol=} {sid=} not monotonic increasing")
            df.sort_index(inplace=True)
            # Remove duplicates
            df = df[~df.index.duplicated(keep="first")]
        # Take days as per calendar
        df = df[df.index.isin(sessions)]
        # 2019-08-13 has a bunch of tickers with multiple day aggs per date
        df = aggregate_multiple_aggs_per_date(df)
        # Check first and last date.
        start_date = df.index[0]
        dates_with_data.add(start_date.date())
        end_date = df.index[-1]
        dates_with_data.add(end_date.date())
        try:
            duplicated_index = df.index.duplicated(keep=False)
            df_with_duplicates = df[duplicated_index]
            if len(df_with_duplicates) > 0:
                print(f" WARNING: {symbol=} {sid=} {len(df_with_duplicates)=}")
                df_with_duplicates.info()
                print(df_with_duplicates)
            # Synch to the official exchange calendar
            df = df.reindex(sessions.tz_localize(None))
        except ValueError as e:
            print(f" ERROR: {symbol=} {sid=} {e}")
            print(f"{start_date=} {end_date=} {sessions[0]=} {sessions[-1]=}")
            df.info()
        # Missing volume and transactions are zero
        df["volume"] = df["volume"].fillna(0)
        df["transactions"] = df["transactions"].fillna(0)
        # Forward fill missing price data (better than backfill)
        df.ffill(inplace=True)
        # Back fill missing data (maybe necessary for before the first day bar)
        df.bfill(inplace=True)
        # There should be no missing data
        if df.isnull().sum().sum() > 0:
            print(f" WARNING: Missing data for {symbol=} {sid=}")

        # The auto_close date is the day after the last trade.
        auto_close_date = end_date + pd.Timedelta(days=1)

        # Add a row to the metadata DataFrame. Don't forget to add an exchange field.
        metadata.loc[sid] = (
            start_date,
            end_date,
            auto_close_date,
            symbol_escaped,
            calendar.name,
            symbol,
        )
        yield sid, df
    return


def polygon_equities_bundle_day(
    environ,
    asset_db_writer,
    minute_bar_writer,
    daily_bar_writer,
    adjustment_writer,
    calendar,
    start_session,
    end_session,
    cache,
    show_progress,
    output_dir,
):
    config = PolygonConfig(
        environ=environ,
        calendar_name=calendar.name,
        start_session=start_session,
        end_session=end_session,
        agg_time="day",
    )

    by_ticker_aggs_arrow_dir = concat_all_aggs_from_csv(config)
    aggregates = pyarrow.dataset.dataset(by_ticker_aggs_arrow_dir)

    # Zipline uses case-insensitive symbols, so we need to convert them to uppercase with a ^ prefix when lowercase.
    # This is because the SQL schema zipline uses for symbols ignores case.
    # We put the original symbol in the asset_name field.
    metadata = pd.DataFrame(
        columns=(
            "start_date",
            "end_date",
            "auto_close_date",
            "symbol",
            "exchange",
            "asset_name",
        )
    )

    table = aggregates.to_table()
    table = table.rename_columns({"ticker": "symbol", "window_start": "day"})
    # Get all the symbols in the table by using value_counts to tabulate the unique values.
    # pyarrow.Table.column returns a pyarrow.ChunkedArray.
    # https://arrow.apache.org/docs/python/generated/pyarrow.ChunkedArray.html#pyarrow.ChunkedArray.value_counts
    symbols = sorted(table.column("symbol").value_counts().field(0).to_pylist())
    symbol_to_sid = {symbol: sid for sid, symbol in enumerate(symbols)}
    dates_with_data = set()

    # Get data for all stocks and write to Zipline
    daily_bar_writer.write(
        process_day_aggregates(
            table=table,
            sessions=calendar.sessions_in_range(start_session, end_session),
            metadata=metadata,
            calendar=calendar,
            symbol_to_sid=symbol_to_sid,
            dates_with_data=dates_with_data,
        ),
        show_progress=show_progress,
    )

    # Write the metadata
    asset_db_writer.write(equities=metadata)

    # Load splits and dividends
    first_start_end = min(dates_with_data)
    last_end_date = max(dates_with_data)
    splits = load_splits(config, first_start_end, last_end_date, symbol_to_sid)
    dividends = load_dividends(config, first_start_end, last_end_date, symbol_to_sid)

    # Write splits and dividends
    adjustment_writer.write(splits=splits, dividends=dividends)


def process_minute_aggregates(
    aggregates,
    sessions,
    metadata,
    calendar,
    ticker_to_sid: dict[str, int],
    dates_with_data: set,
):
    aggregates = aggregates.rename_columns(
        {"ticker": "symbol", "window_start": "timestamp"}
    )
    for symbol in sorted(set(aggregates.column("symbol").to_pylist())):
        if symbol not in ticker_to_sid:
            ticker_to_sid[symbol] = len(ticker_to_sid) + 1
        sid = ticker_to_sid[symbol]
        df = aggregates.filter(
            pyarrow.compute.field("symbol") == pyarrow.scalar(symbol)
        ).to_pandas()
        df = df.set_index("timestamp")
        # The SQL schema zipline uses for symbols ignores case
        if not symbol.isupper():
            df["symbol"] = symbol_to_upper(symbol)
        # # Remove duplicates
        # df = df[~df.index.duplicated()]
        # Take minutes as per calendar
        df = df[df.index.isin(sessions)]
        if len(df) < 2:
            print(f" WARNING: Not enough data for {symbol=} {sid=}")
            continue
        # Check first and last date.
        start_date = df.index[0].date()
        dates_with_data.add(start_date)
        end_date = df.index[-1].date()
        dates_with_data.add(end_date)
        # # Synch to the official exchange calendar
        # df = df.reindex(sessions.tz_localize(None))
        # # Missing volume and transactions are zero
        # df["volume"] = df["volume"].fillna(0)
        # df["transactions"] = df["transactions"].fillna(0)
        # df.info()
        # # Forward fill missing price data (better than backfill)
        # df.ffill(inplace=True)
        # df.info()
        # # Back fill missing data (maybe necessary for the first day)
        # df.bfill(inplace=True)
        # df.info()
        # # There should be no missing data
        # if len(df) != len(sessions):
        #     print(f" WARNING: Missing data for {symbol=} {len(df)=} != {len(sessions)=}")
        # if df.isnull().sum().sum() > 0:
        #     print(f" WARNING: nulls in data for {symbol=} {df.isnull().sum().sum()}")

        # The auto_close date is the day after the last trade.
        ac_date = end_date + pd.Timedelta(days=1)

        # If metadata already has this sid, just extend the end_date and ac_date.
        if sid in metadata.index:
            if metadata.loc[sid, "start_date"] >= start_date:
                print(
                    f" ERROR: {symbol=} {sid=} {metadata.loc[sid, 'start_date']=} >= {start_date=}"
                )
            if metadata.loc[sid, "end_date"] >= start_date:
                print(
                    f" ERROR: {symbol=} {sid=} {metadata.loc[sid, 'end_date']=} >= {end_date=}"
                )
            metadata.loc[sid, "end_date"] = end_date
            metadata.loc[sid, "auto_close_date"] = ac_date
        else:
            # Add a row to the metadata DataFrame. Don't forget to add an exchange field.
            metadata.loc[sid] = (
                start_date,
                end_date,
                ac_date,
                symbol_to_upper(symbol),
                calendar.name,
                symbol,
            )
        # A df with 1 bar crashes zipline/data/bcolz_minute_bars.py", line 747
        # pd.Timestamp(dts[0]), direction="previous"
        if len(df) > 1:
            print(f"\n{symbol=} {sid=} {len(df)=} {df.index[0]=} {df.index[-1]=}")
            yield sid, df
        else:
            print(f" WARNING: Not enough data post reindex for {symbol=} {sid=}")
    return


def polygon_equities_bundle_minute(
    environ,
    asset_db_writer,
    minute_bar_writer,
    daily_bar_writer,
    adjustment_writer,
    calendar,
    start_session,
    end_session,
    cache,
    show_progress,
    output_dir,
):
    config = PolygonConfig(
        environ=environ,
        calendar_name=calendar.name,
        start_session=start_session,
        end_session=end_session,
        agg_time="minute",
    )
    assert calendar == config.calendar

    metadata = pd.DataFrame(
        columns=(
            "start_date",
            "end_date",
            "auto_close_date",
            "symbol",
            "exchange",
            "asset_name",
        )
    )

    # aggregates = generate_all_aggs_from_csv(
    #     config, calendar, start_session, end_session
    # )

    paths, schema, tables = generate_csv_agg_tables(config)

    ticker_to_sid = {}
    dates_with_data = set()

    minute_bar_writer.write(
        process_minute_aggregates(
            tables,
            calendar.sessions_minutes(start_session, end_session),
            metadata,
            calendar,
            ticker_to_sid,
            dates_with_data,
        ),
        show_progress=show_progress,
    )

    # Write the metadata
    asset_db_writer.write(equities=metadata)

    # Load splits and dividends
    first_start_end = min(dates_with_data)
    last_end_date = max(dates_with_data)
    splits = load_splits(config, first_start_end, last_end_date, ticker_to_sid)
    dividends = load_dividends(config, first_start_end, last_end_date, ticker_to_sid)

    # Write splits and dividends
    adjustment_writer.write(splits=splits, dividends=dividends)


def register_polygon_equities_bundle(
    bundlename,
    start_session=None,
    end_session=None,
    calendar_name="XNYS",
    agg_time="day",
    # ticker_list=None,
    # watchlists=None,
    # include_asset_types=None,
):
    if agg_time not in ["day", "minute"]:
        raise ValueError(f"agg_time must be 'day' or 'minute', not '{agg_time}'")
    register(
        bundlename,
        (
            polygon_equities_bundle_minute
            if agg_time == "minute"
            else polygon_equities_bundle_day
        ),
        start_session=start_session,
        end_session=end_session,
        calendar_name=calendar_name,
        # minutes_per_day=390,
        # create_writers=True,
    )


# if __name__ == "__main__":
#     logging.basicConfig(level=logging.WARNING)
#     os.environ["POLYGON_MIRROR_DIR"] = "/Volumes/Oahu/Mirror/files.polygon.io"
#     os.environ["ZIPLINE_ROOT"] = "/Volumes/Oahu/Workspaces/zipline"
#     config = PolygonConfig(
#         environ=os.environ,
#         calendar_name="XNYS",
#         # start_session="2003-10-01",
#         # start_session="2018-01-01",
#         start_session="2023-01-01",
#         # end_session="2023-01-12",
#         end_session="2023-12-31",
#         # end_session="2024-06-30",
#     )
#     splits = load_polygon_splits(config)
#     splits.info()
#     print(splits.head())
#     dividends = load_polygon_dividends(config)
#     dividends.info()
#     print(dividends.head())
#     tickers = set(
#         splits["ticker"].unique().tolist() + dividends["ticker"].unique().tolist()
#     )
#     print(f"{len(tickers)=}")
#     ticker_to_sid = {ticker: sid for sid, ticker in enumerate(tickers)}
#     splits = load_splits(config, ticker_to_sid)
#     splits.info()
#     print(splits.head())
#     dividends = load_dividends(config, ticker_to_sid)
#     dividends.info()
#     print(dividends.head())
