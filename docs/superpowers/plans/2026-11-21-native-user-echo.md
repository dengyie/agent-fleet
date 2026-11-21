# Runner Native User Echo Follow-up

Execute inline with failing evidence and fresh verification. Preserve the
existing native sessions and original Hub events; do not resend their FOLLOW.

## Observed failure

PR106's production Windows Pi task `t-9685f54e2831` completed FIRST and FOLLOW
on native ID `01a1fd44-28af-73ca-85dc-518da3cde81a`. The command
`cmd_78d45ef07490994a375ce49c85dbad0d` succeeded. Hub stored Runner's control
message at sequence118 and Pi's exact `message_end(user)` echo at sequence125,
making one delivered instruction appear twice. FIRST had the same duplicate.
The source records and successful assistant answers remain preserved.

## Fix and verification

- [x] Reproduce initial/follow-up duplicate projection with actual Pi schema.
  Regression initially failed (ten other focused cases passed).
- [x] Record Runner user turns through an explicit Bridge method that allows
  one exact native echo to be suppressed from normalized messages. Archive
  the raw echo as before. Expire the match at the next substantive message or
  tool event, and reset it before each explicit control turn.
- [ ] Verify distinct native users, repeated identical legitimate turns,
  streams without echoes, raw source durability and unchanged standalone
  native capture; run exact CI and the original production session again
  using a separately marked new final-release turn.
- [ ] Rebase fresh main, merge, follow automatic main deployment, signed
  clients, all available Agent paths and final preservation checks. Keep
  Windows Hermes absence and both prior Pi failures explicit.
