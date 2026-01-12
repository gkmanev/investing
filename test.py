import pandas as pd

url = "https://www.cboe.com/available_weeklys/get_csv_download/"
df = pd.read_csv(url)

# Show columns / first rows (format can change over time)
print(df.columns)
print(df.head())

# Example: check if ticker appears anywhere in the CSV
ticker = "AAPL"
has_weeklys = df.astype(str).apply(lambda col: col.str.fullmatch(ticker, case=False, na=False)).any().any()
print(ticker, "has weeklies?" , has_weeklys)