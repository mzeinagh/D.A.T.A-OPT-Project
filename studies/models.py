"""Intentionally empty for now.

This app exists in Phase 1 only to host `management/commands/run_pipeline.py`
— a Django management command needs an installed app to live under, and
`run_pipeline` is Phase 1's own deliverable ("verify end-to-end via a
management command"). The real data model (UploadBatch, Study, StudyPage,
PipelineRun, Answer, LLMCallLog, Question, PromptConfig, and the rest — see
the migration plan) is Phase 2's job, not this file's, yet.
"""
