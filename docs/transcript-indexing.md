# Transcript line indexes

Transcript parsers use the physical JSONL line number as the stable source
location. Empty lines are skipped for parsing but still count toward the next
record's line index. Error messages and generated event identifiers should use
the same one-based display convention so a reported line points to the file a
user opened.

Add a fixture with blank lines whenever parser changes affect line tracking.
