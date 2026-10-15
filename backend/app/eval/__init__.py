"""Model evaluation: test-set collection, admin labelling and scoring.

See docs/MODEL_EVALUATION.md. Nothing here runs in the live detection
path except the optional clean-frame save in inference_worker, which is
gated by settings.eval_capture_raw_frames.
"""
