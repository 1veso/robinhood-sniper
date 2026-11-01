# Confirming a live exit

Live exit alerts do not record a sale. Until you confirm your wallet fill, the
position remains open, its quantity stays unchanged, and watch.py keeps checking
it. This also applies when a pool disappears. Paper-mode behavior is unchanged.

1. Execute the sale yourself in your wallet.
2. From this bot's directory, run the command printed in the alert:

   ```powershell
   .\.venv\Scripts\python.exe .\book.py confirm <mint> <exit_id> <actual_token_qty> <fill_price>
   ```

3. Replace the placeholders with the mint and confirmation ID from the alert,
   the number of tokens actually sold (not dollars or a percentage), and the
   actual average USD price per token for that fill.

For example, confirming 5 tokens at $1.80 means quantity `5` and price `1.80`.
Do not use a displayed market quote in place of your fill price.

The command records only that quantity, using your actual price for proceeds
and realized P&L. If you filled less than the requested amount, the remainder
stays pending and the command prints a new confirmation ID. Use that new ID
for the next actual fill. The take-profit or fading flag changes only once
the requested target has been filled. The article's fractions of original
position size and rule ordering remain unchanged.

Reusing an already confirmed ID is rejected. A new exit reason replaces the
old pending instruction and its ID; if your command is stale, inspect the
current alert or positions.json and confirm the actual fill against the current
pending ID. Do not execute another sale merely because an old ID was rejected.
Zero, negative, non-finite, or excessive quantities and invalid prices are rejected.

The article's `book.py close <mint> <price>` command is retained for compatibility:
it is your explicit confirmation that you sold ALL remaining tokens at that
price. Use `confirm` for alerted fills, especially partial ones, to retain the
exit reason and duplicate-confirmation checks.

No automatic routine may run `confirm` or `close` on your behalf without your
explicit actual-fill confirmation. The bot never signs or executes a swap.
It trusts the fill details you supply and does not independently verify them
on-chain. Its original P&L formula remains quantity × (fill price − entry price).
