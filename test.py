import yfinance as yf

symbol = "aa"
ticker = yf.Ticker(symbol)

# Get all expiration dates
expirations = ticker.options
print("Expirations:", expirations)

# Chain for specific expiration
exp_date = expirations[0]  # Nearest
chain = ticker.option_chain(exp_date)
calls = chain.calls
puts = chain.puts

# Print only strike, bid, ask for calls and puts (full or .head() for top rows)
print("\nCalls (strike, bid, ask):")
print(calls[['strike', 'bid', 'ask']])  # or .head() for preview
print("\nPuts (strike, bid, ask):")
print(puts[['strike', 'bid', 'ask']])  # or .head() for preview
