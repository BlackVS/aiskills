"""The combined-mode instruction: read on a fast LLM profile, review on the deep one.

The wording is the REASONING_PROFILES contract from the consumer README, with the
two profile names filled in per review. A site with hand-saved profiles renders
the same text once into its prompt (site B: block('astra-high', 'astra')); a
site whose profiles come from Auto Reviews appends it per trigger.
"""

TEXT = (
    'Reasoning profiles: this conversation starts on LLM profile `{reading}`\n'
    '(fast reading). Use it ONLY for step 2 and for gathering material in step 3:\n'
    'clone, fetch, the diff, reading the changed files and whatever context they\n'
    'need. As soon as the reading is done and BEFORE you form the frozen scope or\n'
    'write down a single finding, call the `switch_llm` tool with profile_name\n'
    '`{deep}` and reason "review, verification and write-up at maximum\n'
    'reasoning"; do the whole review pass, the VERIFICATION STAGE and step 4 on\n'
    'it and never switch back. If the tool is missing or the switch fails,\n'
    'continue on the current profile.\n'
)


def block(reading, deep):
    if not reading or not deep or reading == deep:
        raise ValueError('Reasoning profiles need two different LLM profiles')
    return TEXT.format(reading=reading, deep=deep)
