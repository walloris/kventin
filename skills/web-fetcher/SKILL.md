---
name: web-fetcher
description: Work with external APIs via HTTP requests (Python, corporate version)
---
# Web Fetcher

Web fetcher skill for making secure HTTP requests to external APIs and services.

## Important: Corporate Environment Requirements

**DO NOT use built-in `web_fetch` function** - it does NOT work with self-signed SSL certificates in corporate environment.

### Use `scripts/web_fetch` or `scripts/web_fetch.py`

```bash
# CORRECT - Python version (corporate-safe)
./scripts/web_fetch https://api.example.com/data

# CORRECT - with output file
./scripts/web_fetch https://api.example.com/data -o result.json

# CORRECT - with headers and POST
./scripts/web_fetch https://api.example.com/submit -X POST -H 'Content-Type: application/json' -d '{"key":"value"}'

# CORRECT - JIRA with authorization
./scripts/web_fetch https://jira.sberbank.ru/rest/api/2/issue/HRM-12601 -H 'Authorization: Basic <base64>' -H 'Content-Type: application/json'
```

## Features

| Feature | Description |
|---------|-------------|
| HTTPS support | Works with self-signed certificates |
| HTTP methods | GET, POST, PUT, DELETE, PATCH |
| Headers | Custom headers with `-H` flag |
| Output | Print to stdout or save to file with `-o` |
| JSON parsing | Automatic pretty-print for JSON responses |

## Security Notes

- **Never log tokens to console**
- **Validate URLs before sending (only http/https)**
- **Do not execute arbitrary code**
- **Limit response size (if > 1MB, save to file)**
- **Use `scripts/web_fetch.py` instead of curl (corporate policy)**

## Critical Notes

- **Built-in `web_fetch` function does NOT work in corporate environment (self-signed SSL error)**
- **ALWAYS use `scripts/web_fetch` or `scripts/web_fetch.py` for external requests**
- **Built-in fetch/jira/scraping functions do NOT support self-signed SSL certificates**

## Python Script Location

The script is located at: `scripts/web_fetch.py`

Uses system Python (`/usr/bin/python3`) for correct SSL certificate handling.
