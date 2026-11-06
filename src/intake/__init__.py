"""Intake: job posts, resumes, application emails and job-board CSVs into the hiring records (SPEC §5, track 9).

- `extract`: pure parsers. File bytes to text, text to resume facts, facts to prefilled answers. No model, no database.
- `jobpost`: a pasted job post goes to Claude for a draft; code keeps only what the post says, and the job stays a
  draft until the recruiter confirms it.
- `load`: the writes, through riley/store.py, into one sandbox.
"""
