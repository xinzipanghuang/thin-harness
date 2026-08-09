# FAQ domain

The FAQ Agent combines the generic harness prompt with document-specific
instructions and owns its PDF helper tools. It uses the shared document tools
for discovery, windowed reading, search, and coverage tracking.

```bash
python -m chat --agent faq --session docs
python examples/faq_run.py "What does the policy say?" --documents ./docs
```

The compatibility import `from agents import FAQAgent` remains supported.
