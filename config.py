CONFIG = {
    "search": {
        "search_urls": [
            "https://au.seek.com/data-analyst-jobs-in-information-communication-technology/in-All-Perth-WA",
            "https://au.seek.com/Business-analyst-jobs-in-information-communication-technology/in-All-Perth-WA?pos=1"
        ],
        "wait_timeout": 12,
        "page_load_wait": 1.2,
        "detail_load_wait": 0.8,
        "flow_retry_limit": 4,
        "click_pause": 0.2,
        "max_flow_steps": 20,
        "max_pages_per_search": 0,
        "debug_host": "127.0.0.1",
        "debug_port": 9222,
    },
    "resume": {
        "resume_file": "resume.pdf",
        "cover_letter_file": "cover_letter.docx",
        "require_on_startup": False,
        "profile_keywords": {
            "must_have": [
                "retail",
                "customer service",
                "cash handling",
            ],
            "preferred": [
                "point of sale",
                "stock",
                "teamwork",
                "communication",
            ],
        },
        "exclude_keywords": [
            "senior manager",
            "phd",
            "registered nurse",
        ],
    },
    "matching": {
        "enabled": False,
        "must_have_weight": 12,
        "preferred_weight": 4,
        "exclude_penalty": 20,
        "must_have_missing_penalty": 10,
        "min_match_score": 20,
    },
    "apply": {
        "session_apply_cap": 0,
        "max_jobs_per_run": 0,
        "quick_apply_only": True,
        "skip_external": True,
        "skip_already_applied": False,
        "auto_submit_enabled": True,
        "skip_on_unanswered_questions": True,
        "wait_for_manual_questions": True,
        "manual_question_timeout_sec": 1800,
        "manual_question_scan_interval_sec": 0.5,
        "force_resume_upload": False,
        "direct_apply_url_fallback": True,
        "script_exe": "Script.exe",
        "script_au3": "Script.au3",
    },
    "logging": {
        "show_match_details": True,
        "show_skip_reasons": True,
    },
}




