# Authority tiers

| Tier | Meaning | Example |
|---|---|---|
| 0 | Act and log. | Copy test inside an existing email step; up to 50 index changes per day. |
| 1 | Act, log, and report in the digest. | Reallocate up to 20% of an envelope; publish copy that passed Compliance. |
| 2 | Needs a human approval row. Non-sticky items expire at 48h and are never executed. | New email sequence; any send over 100 recipients; envelope change; new buyer; taking the lead router live. |
| 3 | Human-only. An agent may prepare, never perform. | Moving money, credentials, contracts, press. |

Rules:
1. The system never proceeds on silence. Pending, expired and rejected all mean "no".
2. Sticky items (a legal, financial or deadline clock is running) never expire;
   they re-escalate on a 24h, 24h, 12h, 12h ladder, alternating approvers.
3. Approver handles come from configuration (`APPROVERS`), never from code.
