"""Café Site Studio v5: Python/FastAPI backend, preserving the v4 browser UI."""
import json
import asyncio
import secrets
import time
import logging
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from openai import AsyncOpenAI
from menu_importer import import_website

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / '.env')
MODEL = os.getenv('OPENAI_MODEL', 'gpt-4.1-mini')
client = AsyncOpenAI(api_key=os.environ['OPENAI_API_KEY']) if os.getenv('OPENAI_API_KEY') else None
app = FastAPI(title='Café Site Studio — Python')
app.mount('/static', StaticFiles(directory=BASE / 'static'), name='static')
templates = Environment(loader=FileSystemLoader(BASE / 'templates'), autoescape=select_autoescape(['html']))
logger = logging.getLogger(__name__)

STYLES = {'cozy and welcoming': 'cozy', 'modern and minimal': 'modern',
          'bright and playful': 'playful', 'elegant and calm': 'elegant'}
TONES = {'natural', 'friendly', 'concise', 'polished'}
DATA_IMAGE = re.compile(r'^data:image/(?:jpeg|png|webp);base64,[A-Za-z0-9+/=]+$')
HEX = re.compile(r'^#[0-9A-Fa-f]{6}$')


def clean(value, limit=500):
    return str(value if value is not None else '').strip()[:limit]


def safe_image(value):
    if not isinstance(value, str):
        return ''
    if len(value) <= 9_000_000 and DATA_IMAGE.fullmatch(value):
        return value
    try:
        parts = urlsplit(value)
        if parts.scheme in ('https', 'http') and parts.netloc:
            return value[:2048]
    except ValueError:
        pass
    return ''


def safe_color(value):
    return value if isinstance(value, str) and HEX.fullmatch(value) else '#006241'


def mix(a, b, ratio):
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return '#' + ''.join(f'{round(x * (1 - ratio) + y * ratio):02x}' for x, y in zip(ca, cb))


def luminance(hex_color):
    c = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    c = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def theme_from_accent(accent):
    primary = accent
    for _ in range(8):
        if luminance(primary) <= .15:
            break
        primary = mix(primary, '#000000', .15)
    return dict(primary=primary, soft=mix(accent, '#ffffff', .87),
                pale=mix(accent, '#ffffff', .955), hero=mix(accent, '#ffffff', .8),
                ink=mix(primary, '#141a19', .65), footer=mix(primary, '#091712', .55),
                border=mix(accent, '#ffffff', .7))


@app.get('/')
async def home():
    return FileResponse(BASE / 'static' / 'index.html', media_type='text/html')


@app.get('/api/health')
async def health():
    return {'status': 'ok', 'ai_configured': bool(client)}




IMPORT_JOBS = {}
IMPORT_SEMAPHORE = asyncio.Semaphore(1)


@app.post('/api/import-jobs')
async def start_import_job(request: Request):
    payload = await request.json()
    url = payload.get('url', '') if isinstance(payload, dict) else ''
    if not isinstance(url, str) or not url.strip() or len(url) > 2048:
        raise HTTPException(400, 'Enter a valid restaurant website URL.')
    if IMPORT_SEMAPHORE.locked():
        raise HTTPException(429, 'An import is already running. Finish it before starting another.')
    job_id = secrets.token_urlsafe(16)
    
    IMPORT_JOBS.clear()
    job = {'state': 'running', 'stage': 'Starting', 'percent': 0,
           'message': 'Preparing website importer', 'pagesChecked': 0,
           'productsFound': 0, 'created': time.time()}
    IMPORT_JOBS[job_id] = job

    async def perform():
        async with IMPORT_SEMAPHORE:
            try:
                def update(**values):
                    job.update(values)
                result = await import_website(url.strip(), progress=update)
                job.update(state='complete', stage='Complete', percent=100,
                           message='Import finished', result=result)
            except Exception:
                logger.exception('Import job failed')
                job.update(state='failed', stage='Failed', error='Import failed. Check Python terminal for details.')

    asyncio.create_task(perform())
    return {'jobId': job_id}


@app.get('/api/import-jobs/{job_id}')
async def import_job_status(job_id: str):
    job = IMPORT_JOBS.get(job_id)
    if not job:
        raise HTTPException(404, 'Import not found. Restart the import.')
    return {key: value for key, value in job.items() if key != 'created'}


@app.post('/api/import-menu')
async def import_menu(request: Request):
    payload = await request.json()
    url = payload.get('url', '') if isinstance(payload, dict) else ''
    if not isinstance(url, str) or len(url) > 2048:
        raise HTTPException(400, 'Enter a valid website URL.')
    try:
        result = await import_website(url.strip())
        logger.info('Import complete: %d items, %d pages', len(result.get('items', [])), result.get('pagesChecked', 0))
        return result
    except HTTPException:
        raise
    except Exception:
        logger.exception('Unexpected import failure')
        raise HTTPException(500, 'Import failed; check the Python server logs.')


@app.post('/api/generate')
async def generate(request: Request):
    if client is None:
        raise HTTPException(503, 'Add OPENAI_API_KEY to .env and restart the server.')
    if int(request.headers.get('content-length', '0') or '0') > 35_000_000:
        raise HTTPException(413, 'Images are too large. Try fewer or smaller photos.')
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, 'Invalid JSON request.')
    if not isinstance(payload, dict):
        raise HTTPException(400, 'Expected a website configuration object.')

    business = re.sub(r'^\s*menu\s*:\s*', '', clean(payload.get('businessName'), 80), flags=re.I).strip()
    notes = clean(payload.get('notes'), 600)
    raw_menu = payload.get('menu')
    if not isinstance(raw_menu, list):
        raise HTTPException(400, 'Menu must be a list of items.')
    menu = []
    if len(raw_menu) > 600:
        raise HTTPException(400, "This version supports up to 600 menu items; no items were dropped.")
    for item in raw_menu:
        if not isinstance(item, dict):
            continue
        product = dict(category=clean(item.get('category'), 45), subcategory=clean(item.get('subcategory'), 70), name=clean(item.get('name'), 85),
                       description=clean(item.get('description'), 180), image=safe_image(item.get('image')),
                       categoryOrder=int(item.get('categoryOrder', 99999)),
                       subcategoryOrder=int(item.get('subcategoryOrder', 99999)),
                       productOrder=int(item.get('productOrder', 99999)))
        if product['category'] and product['name']:
            menu.append(product)
    if len(business) < 2:
        raise HTTPException(400, 'Enter a business name (at least 2 characters).')
    if not menu:
        raise HTTPException(400, 'Add at least one menu item with a category and name.')

    gallery_input = payload.get('environmentImages')
    if not isinstance(gallery_input, list):
        gallery_input = []
    photos = [value for value in (safe_image(x) for x in gallery_input) if value]
    if len(photos) > 100:
        raise HTTPException(400, 'Please use 100 or fewer gallery photos in this version.')
    color = safe_color(payload.get('color'))
    vibe = payload.get('vibe') if payload.get('vibe') in STYLES else 'cozy and welcoming'

    try:
        result = await client.responses.create(
            model=MODEL,
            instructions=("Write concise, accurate café website copy. Return JSON only with keys "
                          "'tagline' and 'about' (strings). Use supplied facts only; never invent "
                          "awards, addresses, prices, opening hours, testimonials, products, or locations. "
                          "Tagline under 110 characters and about under 350. Match the requested vibe."),
            input=json.dumps({'businessName': business, 'vibe': vibe, 'notes': notes,
                              'menu': [{k: m[k] for k in ('category', 'name', 'description')} for m in menu]}, ensure_ascii=False),
            text={'format': {'type': 'json_schema', 'name': 'website_copy', 'strict': True,
                             'schema': {'type': 'object', 'additionalProperties': False,
                                        'properties': {'tagline': {'type': 'string'}, 'about': {'type': 'string'}},
                                        'required': ['tagline', 'about']}}}
        )
        copy = json.loads(result.output_text)
        tagline, about = clean(copy.get('tagline'), 110), clean(copy.get('about'), 350)
        if len(str(copy.get('about','')).strip()) > 350:
            endings = [about.rfind(c) for c in '.!?']
            last = max(endings)
            if last > 80: about = about[:last + 1]
            else: about = about.rsplit(' ', 1)[0].rstrip(' ,;:') + '.'
        if not tagline or not about:
            raise ValueError('AI returned incomplete website content.')
    except Exception:
        logger.exception('Generation failed')
        raise HTTPException(502, 'AI generation failed. Check your API key/model and retry.')

    menu.sort(key=lambda p:(p['categoryOrder'],p['subcategoryOrder'],p['productOrder']))
    groups_by_name = {}
    for item in menu:
        groups_by_name.setdefault(item['category'], {}).setdefault(item['subcategory'], []).append(item)
    groups = [{'name': category, 'sections': [{'name': subcategory, 'items': items} for subcategory, items in sections.items()]} for category, sections in groups_by_name.items()]
    site = dict(businessName=business, tagline=tagline, about=about, menu=menu,
                color=color, vibe=vibe, environmentImages=photos)
    html = templates.get_template('cafe.html').render(
        business_name=business, tagline=tagline, about=about, theme=theme_from_accent(color),
        style_id=STYLES[vibe], groups=groups, environment_images=photos)
    return {'html': html, 'site': site}


@app.post('/api/revise')
async def revise(request: Request):
    if client is None:
        raise HTTPException(503, 'Configure OPENAI_API_KEY to enable AI rewriting.')
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, 'Invalid JSON request.')
    if not isinstance(payload, dict):
        raise HTTPException(400, 'Invalid review request.')
    review = clean(payload.get('review'), 451)
    tone = payload.get('tone') if payload.get('tone') in TONES else 'natural'
    if not review:
        raise HTTPException(400, 'Please enter a review.')
    if len(review) > 450:
        raise HTTPException(400, 'Keep the review under 450 characters.')
    instructions = ("You revise real customer café reviews. Correct mistakes and meaningfully rephrase "
                    "the ENTIRE review without changing the customer's opinion, including negatives. "
                    "Never invent products, experiences, timing or service details. "
                    "Natural: relaxed everyday language and meaningful paraphrasing. "
                    "Friendly: warmer conversational language without exaggeration. "
                    "Polished: smooth and refined, still authentic. Short: noticeably shorter while "
                    f"retaining central opinions. Apply the requested tone: {tone}. "
                    "For Natural/Friendly/Polished, preserve the appropriate amount of detail. "
                    "Return only the rewritten review.")
    try:
        result = await client.responses.create(model=MODEL, instructions=instructions, input=review)
        revised = (result.output_text or '').strip()
        if not revised:
            raise ValueError('AI returned an empty review.')
        return {'revisedReview': revised}
    except Exception:
        logger.exception('Review rewriting failed')
        raise HTTPException(502, 'Review rewriting failed. Check server configuration and retry.')
