"""CLI entrypoint to launch the Sanskrit PR reviewer via ``python -m pull_requests_san``."""

from pull_requests_san.pr_review_gui import main

if __name__ == "__main__":
  raise SystemExit(main())
