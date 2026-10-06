#!/usr/bin/env python3
"""Hugging Face Spaces entry point (thin launcher).

The actual Gradio app lives in web/app.py. This file exists so the Space
can run `python app.py` from the repo root. Additive only.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from web.app import demo  # noqa: E402

if __name__ == "__main__":
    demo.launch(server_name="0.0.0.0", server_port=7860)
