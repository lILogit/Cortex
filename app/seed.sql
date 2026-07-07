-- CORTEX starter data. Idempotent: INSERT OR IGNORE only.

INSERT OR IGNORE INTO meta (key, value) VALUES
  ('starter_tags', '["car","home","work","shop","finance","health","family"]');

INSERT OR IGNORE INTO meta (key, value) VALUES
  ('help_text', 'CORTEX — personal backlog.
Plain text = capture (Czech or English). Examples:
  koupit dalnicni znamku, expirace 10.07.2026 important #car
  idea: causal wiki export button

Commands:
  /?? <query>          search items
  /reveal TKN-XXXXXXXX de-anonymize a vaulted value
  /brief               daily brief now
  /week                weekly review now
  /done <id>           mark item done (archives it)
  /snooze <id> [days]  snooze item (default 7)
  /help                this text');
