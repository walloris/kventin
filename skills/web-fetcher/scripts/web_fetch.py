#!/usr/bin/env python3
"""
web_fetch - Python-аналог для извлечения содержимого веб-страниц и API
Безопасный вариант для корпоративной среды (использует urllib, разрешён политикой)

Использование:
    ./tools/web_fetch <URL> [OPTIONS]
    ./tools/web_fetch.py <URL> [OPTIONS]

Примеры:
    ./tools/web_fetch https://api.example.com/data
    ./tools/web_fetch https://api.example.com/data -o result.json
    ./tools/web_fetch https://api.example.com/data -H "Authorization: Bearer token" -X POST -d '{"key":"value"}'

ВАЖНО: Использует системный Python (/usr/bin/python3) для корректной работы с самоподписанными SSL-сертификатами.
"""

import argparse
import urllib.request
import urllib.error
import json
import sys
import ssl
import os

def main():
    parser = argparse.ArgumentParser(description='Fetch data from URLs (corporate-safe version)')
    parser.add_argument('url', help='URL to fetch')
    parser.add_argument('-o', '--output', help='Save output to file')
    parser.add_argument('-H', '--header', action='append', help='Add HTTP header (can be used multiple times)')
    parser.add_argument('-X', '--method', default='GET', choices=['GET', 'POST', 'PUT', 'DELETE', 'PATCH'], help='HTTP method')
    parser.add_argument('-d', '--data', help='Data to send (for POST/PUT/PATCH)')
    parser.add_argument('-s', '--silent', action='store_true', help='Silent mode (no progress)')
    parser.add_argument('-v', '--verbose', action='store_true', help='Verbose mode (show headers)')
    
    args = parser.parse_args()
    
    # Настройка заголовков
    headers = {}
    if args.header:
        for h in args.header:
            if ':' not in h:
                print(
                    f"Invalid header (expected 'Name: value'): {h!r}",
                    file=sys.stderr,
                )
                sys.exit(2)
            key, value = h.split(':', 1)
            key = key.strip()
            if not key:
                print(
                    f"Invalid header (empty name): {h!r}",
                    file=sys.stderr,
                )
                sys.exit(2)
            headers[key] = value.strip()
    
    # Для POST/PUT/PATCH добавляем Content-Type если не задан
    if args.method in ['POST', 'PUT', 'PATCH'] and 'Content-Type' not in headers:
        headers['Content-Type'] = 'application/json'
    
    # Создание запроса
    req = urllib.request.Request(
        args.url,
        data=args.data.encode('utf-8') if args.data else None,
        headers=headers,
        method=args.method
    )
    
    try:
        # Отключение валидации SSL (как у curl -k)
        # Это нужно для работы с корпоративными JIRA, где используются самоподписанные сертификаты
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        
        with urllib.request.urlopen(req, context=ctx) as response:
            content = response.read().decode('utf-8')
            
            if args.verbose:
                print(f"Status: {response.status}")
                print("Headers:")
                for key, value in response.headers.items():
                    print(f"  {key}: {value}")
                print()
            
            if args.output:
                with open(args.output, 'w', encoding='utf-8') as f:
                    f.write(content)
                if not args.silent:
                    print(f"Saved to {args.output}")
            else:
                if not args.silent:
                    # Pretty print JSON if possible
                    try:
                        json_obj = json.loads(content)
                        print(json.dumps(json_obj, indent=2, ensure_ascii=False))
                    except json.JSONDecodeError:
                        print(content)
                        
    except urllib.error.HTTPError as e:
        print(f"HTTP Error {e.code}: {e.reason}", file=sys.stderr)
        if e.fp:
            print(e.fp.read().decode('utf-8'), file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"URL Error: {e.reason}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == '__main__':
    main()
