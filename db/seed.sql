-- Seed data for SQLite. Idempotent.

INSERT INTO cash
  (ts, account, starting_capital, current_balance, high_watermark, withdrawals,
   mode, daily_pnl, daily_loss_halt, notes)
SELECT
  strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'PAPER-001', 20000.00, 20000.00, 20000.00, 0,
  'paper', 0, 1000.00, 'initial seed'
WHERE NOT EXISTS (SELECT 1 FROM cash);

INSERT OR IGNORE INTO strategies
  (id, name, class_path, tier, enabled, params, ts_created, notes)
VALUES (
  'iron_condor_range_lo',
  'LO Iron Condor (range-bound)',
  'strategies.iron_condor_range.IronCondorRange',
  'paper',
  1,
  '{"trading_class":"LO","target_dte_min":5,"target_dte_max":14,"short_put_delta":-0.20,"long_put_offset":5,"short_call_delta":0.20,"long_call_offset":5,"min_credit_pct_of_width":0.20,"vol_filter_min_iv":0.40,"profit_target_pct":0.50,"stop_loss_pct":1.00,"time_stop_dte":3,"max_concurrent":3}',
  strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
  'phase 1 default strategy'
);

INSERT INTO patterns (name, description, category, weight, active, ts_created, notes)
SELECT * FROM (
  SELECT 'Walk limit price 1-2 cents on wide bid/ask' AS name,
         'When ask/bid spread is wide, walking the limit improves fill probability.' AS description,
         'execution' AS category, 0.95 AS weight, 1 AS active,
         strftime('%Y-%m-%dT%H:%M:%fZ', 'now') AS ts_created, 'user-provided, high trust' AS notes
  UNION ALL
  SELECT 'Front-month vol >100% almost always crushes within 3 days post-event',
         'Selling premium has positive EV in this regime.',
         'vol_regime', 0.65, 1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'user-provided, medium trust'
  UNION ALL
  SELECT '5%+ daily drop often produces 2%+ next-day bounce',
         'Long upside verticals have positive EV after a flush.',
         'mean_reversion', 0.55, 1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'user-provided, medium trust'
)
WHERE NOT EXISTS (SELECT 1 FROM patterns);
