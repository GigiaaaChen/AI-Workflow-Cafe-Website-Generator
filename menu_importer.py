"""Best-effort public website importer; does not bypass access restrictions."""
import asyncio
import base64
from io import BytesIO
import os
from pypdf import PdfReader
from browser_fetch import render_page, explore_page
import ipaddress
import json
import re
import socket
from collections import OrderedDict, deque
from urllib.parse import urljoin, urlsplit, urldefrag

import httpx
from bs4 import BeautifulSoup
from PIL import Image, ImageOps
from fastapi import HTTPException
from ai_extractor import ai_extract, ai_client, page_evidence, MAX_AI_PAGES

MAX_PAGES = 45
MAX_BROWSER_PAGES = 24
MAX_PDF_BYTES = 6_000_000
MAX_PDF_PAGES = 8
MAX_GALLERY = 12
MAX_FETCH_IMAGES = 450
MAX_PRODUCTS = 600
MAX_BYTES = 2_000_000
HEADERS = {'User-Agent': 'CafeSiteStudioDemo/0.6 (public menu importer)', 'Accept': 'text/html,application/xhtml+xml,application/pdf'}


def verified_url(raw):
    try:
        p = urlsplit(raw)
        if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password or p.port not in (None, 80, 443):
            raise ValueError()
        hostname = p.hostname.lower().rstrip('.')
        if hostname in ('localhost',) or hostname.endswith(('.local', '.internal')):
            raise ValueError()
        infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
        for entry in infos:
            address = ipaddress.ip_address(entry[4][0])
            if not address.is_global:
                raise ValueError()
        return raw
    except (ValueError, OSError, TypeError):
        raise HTTPException(400, 'Enter a publicly accessible HTTP(S) restaurant website URL.')


def site_root(url):
    host = urlsplit(url).hostname or ''
    if host.startswith('www.'):
        host = host[4:]
    return host


def same_site(url, root):
    """Match normalized hostnames, not URL paths; www alias only."""
    def host(value):
        value = str(value or '').strip()
        p = urlsplit(value if '://' in value else 'https://' + value)
        name = (p.hostname or '').lower().rstrip('.')
        return name[4:] if name.startswith('www.') else name
    return bool(host(url)) and host(url) == host(root)


def plain(value, limit=180):
    return re.sub(r'\s+', ' ', str(value or '')).strip()[:limit]


def image_from(element, base):
    """Read the actual picture element: never substitute the enclosing product link."""
    if not element:
        return ''
    img = element.find('img')
    if not img:
        return ''
    raw = (img.get('data-src') or img.get('data-original') or img.get('data-lazy-src')
           or img.get('data-srcset') or img.get('srcset') or img.get('src') or '')
    
    if ',' in raw:
        raw = raw.split(',')[0]
    if ' ' in raw.strip() and not raw.startswith('data:'):
        raw = raw.strip().split()[0]
    if not raw or raw.startswith('data:image/svg'):
        picture = img.find_parent('picture')
        source = picture.find('source') if picture else None
        raw = (source.get('srcset') or source.get('data-srcset') or '').split(',')[0].strip().split(' ')[0] if source else ''
    if not raw or raw.startswith(('data:', 'blob:')):
        return ''
    u = urljoin(base, raw.strip())
    return u if urlsplit(u).scheme in ('https', 'http') else ''


def plausible_image_url(value):
    """Reject known document/product routes without rejecting extensionless CDNs."""
    if not value or not isinstance(value, str): return False
    p = urlsplit(value)
    if p.scheme not in ('https','http') or not p.hostname: return False
    path = p.path.lower()
    if re.search(r'\.(?:html?|pdf|json|js|css)(?:$|/)', path): return False
    if re.search(r'/(?:en-[a-z]{2}/)?products?/[a-z0-9_-]+/[^/]+$', path) and not re.search(r'\.(?:png|jpe?g|webp|gif|avif)$',path):
        
        return False
    return True


def collect_product_images(soup, base_url):
    """Find actual img/picture URLs attached to an image's explicit product label.

    This does not infer a product image from arbitrary nearby photography.
    """
    candidates = {}
    for img in soup.select('main img, [role="main"] img, article img, figure img'):
        alt = plain(img.get('alt') or img.get('aria-label') or '', 180)
        wrapper = img.find_parent(['a', 'article', 'figure'])
        label_tag = wrapper.select_one('h2,h3,h4,[itemprop="name"]') if wrapper else None
        labels = [alt]
        if label_tag:
            labels.append(plain(label_tag.get_text(' ', strip=True), 180))
        image_url = image_from(img.parent if img.parent else img, base_url)
        if not plausible_image_url(image_url):
            continue
        for label in labels:
            if not label: continue
            normalized = normalize_name(label).casefold()
            if normalized:
                candidates.setdefault(normalized, image_url)
    return candidates


def image_for_product(candidates, product_name):
    wanted = normalize_name(product_name).casefold()
    if wanted in candidates:
        return candidates[wanted]
    
    for label, link in candidates.items():
        if label.endswith(' ' + wanted) and len(label) - len(wanted) < 20:
            return link
    return ''


def normalize_name(name):
    """Remove exact repeated title, never remove legitimate word repetitions."""
    name = plain(name, 170)
    words = name.split()
    if len(words) % 2 == 0 and words[:len(words)//2] == words[len(words)//2:]:
        return ' '.join(words[:len(words)//2])[:85]
    return name[:85]

def navigation_names(soup):
    """Names linked to category pages, not product links. Returns per-page labels."""
    found = set()
    scope = soup.select_one('main, [role="main"]') or soup
    for a in scope.select('a[href]'):
        path = urlsplit(a.get('href') or '').path.lower()
        if not path or '/product/' in path or '/item/' in path: continue
        if a.find(['h2','h3','h4','img']) or a.get('role') == 'tab':
            heading = a.find(['h2','h3','h4'])
            text = plain(heading.get_text(' ',strip=True) if heading else a.get_text(' ',strip=True),85)
            if text: found.add(text.casefold())
    return found

def element_products(soup, url, category_hint='Menu'):
    """Extract only cards with a distinct product name and associated image."""
    result=[]
    navigation=navigation_names(soup)
    scope=soup.select_one('main, [role="main"]') or soup
    selectors='[itemtype*="MenuItem"], [data-menu-item], .menu-item, .menu__item, [data-testid*="product"], a[href*="/product/"], a[href*="/item/"]'
    seen=set()
    for card in scope.select(selectors):
        
        if card.find_parent(attrs={'data-menu-item':True}): continue
        title=card.select_one('[itemprop="name"], h2, h3, h4, .item-name, .menu-item-name')
        if not title: continue
        name=normalize_name(title.get_text(' ',strip=True))
        if not name or name.casefold() in navigation: continue
        href=card.get('href') or (card.find('a',href=True) or {}).get('href','')
        path=urlsplit(href).path.lower()
        if href and path and not any(t in path for t in ('/product/','/item/')) and not card.has_attr('data-menu-item') and not 'menu-item' in ' '.join(card.get('class',[])):
            continue
        key=(name.casefold(),urljoin(url,href))
        if key in seen: continue
        seen.add(key)
        image=image_from(card,url)
        desc=card.select_one('[itemprop="description"], p, .description')
        result.append(dict(category=category_hint,name=name,description=plain(desc.get_text(' ',strip=True) if desc else ''),image=image,sourceUrl=urljoin(url,href) if href else url))
    return result


def page_hierarchy(page_url, page_context, navigation):
    """Use breadcrumb or explicit parent/child category links; flat menus remain flat."""
    crumbs = [x for x in page_context.get('breadcrumb', []) if x and x.casefold() not in ('home', 'menu')]
    if len(crumbs) >= 2:
        return crumbs[-2], crumbs[-1]
    if len(crumbs) == 1:
        return crumbs[0], ''
    
    info = navigation.get(page_url)
    if info:
        parent, label = (info.get('parent',''), info.get('label','')) if isinstance(info, dict) else info
        if parent and label and parent.casefold() != label.casefold():
            return parent, label
        return label or page_context.get('heading') or 'Menu', ''
    return (page_context.get('heading') if (page_context.get('heading') or '').casefold() not in ('view menu','our menu','products') else '') or 'Menu', ''


def navigation_cards(soup, url, root):
    """Link cards in document order; locate owning section via nearby headings.

    The site decides labels and positions. No restaurant/category name is hardcoded.
    """
    scope = soup.select_one('main, [role="main"]') or soup
    result = OrderedDict()
    for anchor in scope.select('a[href]'):
        raw = anchor.get('href', '')
        target = urldefrag(urljoin(url, raw))[0]
        if not same_site(target, root) or target == url:
            continue
        path = urlsplit(target).path.lower()
        if any(x in path for x in ('/product/', '/item/')):
            continue
        label_tag = anchor.select_one('h2,h3,h4,[role="heading"]')
        label = plain(label_tag.get_text(' ', strip=True) if label_tag else anchor.get_text(' ', strip=True), 85)
        if not label or not (label_tag or anchor.find('img')):
            continue
        
        heading = anchor.find_previous(['h1','h2'])
        while heading is not None and (heading.find_parent('a') is not None or anchor in heading.parents):
            heading = heading.find_previous(['h1','h2'])
        parent = plain(heading.get_text(' ', strip=True), 85) if heading else ''
        if parent.casefold() == label.casefold() or parent.casefold() in ('menu', 'our menu'):
            parent = ''
        if target not in result:
            result[target] = {'label': label, 'parent': parent, 'order':len(result)}
    return result


def merge_navigation(navigation, target, meta):
    """Later category pages can clarify a leaf link first seen on menu home."""
    previous = navigation.get(target)
    if not previous:
        navigation[target] = dict(meta)
    elif meta.get('parent') and not previous.get('parent') and meta['parent'].casefold() != previous.get('label','').casefold():
        previous['parent'] = meta['parent']


def navigation_metadata(page_url, context, navigation):
    """Resolve source navigation ancestry; a menu link is not a product."""
    info = navigation.get(page_url, {})
    category, subcategory = page_hierarchy(page_url, context, navigation)
    if info:
        parent = info.get('parent', '')
        label = info.get('label', '')
        if parent and parent.casefold() != label.casefold():
            category, subcategory = parent, label
        elif label:
            category, subcategory = label, ''
    if category.casefold() == subcategory.casefold():
        subcategory = ''
    return category, subcategory


def _key(url):
    """Canonicalize page identity without relying on a restaurant-specific URL."""
    parts = urlsplit(url or '')
    return (parts.hostname or '').lower().removeprefix('www.'), parts.path.rstrip('/') or '/'


def menu_tree(navigation, home_url):
    """Map discovered menu-section URLs to (main, sub) using source link ancestry.

    The path hierarchy is used only to relate links and never to invent
    products or category names. Labels always come from discovered cards.
    """
    known = OrderedDict()
    for url, meta in navigation.items():
        if not isinstance(meta, dict) or not plain(meta.get('label')):
            continue
        known[_key(url)] = (url, meta)
    tree = OrderedDict()
    home_path = _key(home_url)[1]
    for identity, (url, meta) in known.items():
        label = plain(meta['label'], 70)
        parent = plain(meta.get('parent'), 70)
        path = identity[1]
        ancestors = []
        for other_key, (_, ancestor_meta) in known.items():
            ancestor_path = other_key[1]
            if other_key[0] == identity[0] and ancestor_path != path and path.startswith(ancestor_path.rstrip('/') + '/'):
                ancestors.append((len(ancestor_path), plain(ancestor_meta['label'],70)))
        ancestors.sort(reverse=True)
        
        
        
        if ancestors:
            main = ancestors[-1][1]
            sub = label if main.casefold() != label.casefold() else ''
        elif parent and parent.casefold() != label.casefold():
            main, sub = parent, label
        else:
            main, sub = label, ''
        
        
        if path == home_path:
            continue
        tree[identity] = (main, sub)
    return tree


def resolve_menu_hierarchy(page_url, navigation, home_url):
    return menu_tree(navigation, home_url).get(_key(page_url))


def reconcile_menu_tree(products, navigation, home_url, section_cards=None):
    """Keep product records, strip navigation cards, preserve source images.

    Section cards are identified by an actual link leading to another menu
    section, not by a fixed list of category words. A product link always wins.
    """
    tree = menu_tree(navigation, home_url)
    section_labels = {plain(meta.get('label'),85).casefold() for meta in navigation.values() if meta.get('label')}
    section_cards = section_cards or set()
    section_urls = set(tree)
    unique = OrderedDict()
    removed = 0
    for original in products:
        item = dict(original)
        name = normalize_name(item.get('name'))
        source = item.get('sourceUrl','')
        page_url = item.get('pageUrl') or source
        path = urlsplit(source).path.lower()
        explicit = '/product/' in path or '/item/' in path
        
        
        is_navigation = (name.casefold(), _key(source)) in section_cards
        if not explicit and (is_navigation or (name.casefold() in section_labels and not item.get('description'))):
            removed += 1
            continue
        hierarchy = tree.get(_key(page_url)) or tree.get(_key(source))
        if hierarchy:
            item['category'], item['subcategory'] = hierarchy
        elif not explicit:
            
            
            pass
        if item.get('category','').casefold() == item.get('subcategory','').casefold():
            item['subcategory'] = ''
        item['name'] = name
        key = (item.get('category','').casefold(), item.get('subcategory','').casefold(),name.casefold())
        if key in unique:
            prior=unique[key]
            if not prior.get('image') and item.get('image'): prior['image']=item['image']
            if not prior.get('description') and item.get('description'): prior['description']=item['description']
        else:
            unique[key]=item
    return list(unique.values()), removed


def apply_original_order(products, navigation, home_url):
    """Reconstruct menu order independently of network/AI completion order."""
    
    category_rank = {}
    subcategory_rank = {}
    for target, info in navigation.items():
        main = info.get('parent') or info.get('label') or ''
        sub = info.get('label') if info.get('parent') else ''
        if main and main.casefold() not in category_rank:
            category_rank[main.casefold()] = len(category_rank)
        if sub:
            key = (main.casefold(), sub.casefold())
            if key not in subcategory_rank:
                subcategory_rank[key] = len([x for x in subcategory_rank if x[0] == main.casefold()])
    for index, item in enumerate(products):
        cat = item.get('category','')
        sub = item.get('subcategory','')
        if cat.casefold() == sub.casefold():
            item['subcategory'] = ''
            sub = ''
        item['categoryOrder'] = category_rank.get(cat.casefold(), len(category_rank) + index)
        item['subcategoryOrder'] = subcategory_rank.get((cat.casefold(),sub.casefold()), 0 if not sub else 999)
        item['productOrder'] = int(item.get('productOrder',index))
    return sorted(products, key=lambda p:(p['categoryOrder'],p['subcategoryOrder'],p['productOrder']))


def add(items, category, name, desc='', image='', source='', subcategory='', product_order=None):
    name, category = normalize_name(name), plain(category, 70)
    if not name or len(name) < 3 or not category or len(items) >= MAX_PRODUCTS:
        return
    key = (category.casefold(), plain(subcategory, 70).casefold(), name.casefold())
    if key in items:
        old = items[key]
        if not old.get('image') and image: old['image'] = image
        if not old.get('description') and desc: old['description'] = plain(desc)
        return
    items[key] = {'category': category, 'subcategory': plain(subcategory, 70), 'name': name, 'description': plain(desc), 'image': image, 'sourceUrl': source, 'pageUrl': source, 'productOrder': product_order if product_order is not None else len(items)}


def parse_ld(node, items, url, category='Menu'):
    if isinstance(node, list):
        for child in node:
            parse_ld(child, items, url, category)
        return
    if not isinstance(node, dict):
        return
    typ = node.get('@type', '')
    types = typ if isinstance(typ, list) else [typ]
    label = plain(node.get('name'), 70)
    if any(t in ('Menu', 'MenuSection') for t in types):
        category = label if any(t == 'MenuSection' for t in types) and label else category
    if any(t in ('MenuItem', 'Product') for t in types):
        pic = node.get('image') or ''
        if isinstance(pic, list): pic = pic[0] if pic else ''
        if isinstance(pic, dict): pic = pic.get('url', '')
        add(items, category, label, node.get('description'), urljoin(url, pic) if isinstance(pic, str) and pic else '', url)
    for k in ('hasMenu', 'hasMenuSection', 'hasMenuItem', 'itemListElement', '@graph', 'mainEntity', 'menu'):
        if k in node:
            parse_ld(node[k], items, url, category)
    if node.get('item') and any(t == 'ListItem' for t in types):
        parse_ld(node['item'], items, url, category)


def find_product_detail_links(soup, base_url, root):
    found = {}
    scope = soup.select_one('main, [role="main"]') or soup
    for link in scope.select('a[href]'):
        target = urljoin(base_url, link.get('href', ''))
        path = urlsplit(target).path.lower()
        if not same_site(target, root) or not re.search(r'/products?/|/items?/', path):
            continue
        label_node = link.select_one('[itemprop="name"],h1,h2,h3,h4')
        label = normalize_name(label_node.get_text(' ', strip=True) if label_node else link.get_text(' ', strip=True))
        if label and len(label) >= 3:
            found.setdefault(label.casefold(), target)
    return found


def detail_description(html, name):
    soup = BeautifulSoup(html, 'html.parser')
    heading = soup.select_one('main h1, [role="main"] h1, h1')
    page_name = plain(heading.get_text(' ', strip=True), 150) if heading else ''
    if not page_name or normalize_name(name).casefold() not in page_name.casefold():
        return ''
    for selector in ('[itemprop="description"]', 'main [class*="description"]', 'main [class*="Description"]', '[role="main"] [class*="description"]'):
        for element in soup.select(selector)[:8]:
            value = plain(element.get_text(' ', strip=True), 500)
            if len(value) >= 28 and normalize_name(name).casefold() not in value.casefold()[:100]:
                return value[:180]
    main = soup.select_one('main, [role="main"]') or soup
    for paragraph in main.select('p')[:18]:
        value = plain(paragraph.get_text(' ', strip=True), 500)
        if len(value) >= 35 and not any(key in value.casefold() for key in ('privacy policy', 'cookie settings', 'terms and conditions', 'allergen information')):
            return value[:180]
    return ''


def extract_business(soup, page_url):
    """Use publisher-provided organization metadata, then on-page branding."""
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            raw = json.loads(script.string or script.get_text())
        except (ValueError, TypeError):
            continue
        stack = [raw]
        while stack:
            obj = stack.pop()
            if isinstance(obj, list):
                stack.extend(obj)
            elif isinstance(obj, dict):
                kinds = obj.get('@type', [])
                kinds = [kinds] if isinstance(kinds, str) else kinds
                if any(t in kinds for t in ('Restaurant', 'CafeOrCoffeeShop', 'FoodEstablishment', 'LocalBusiness', 'Organization')):
                    name = plain(obj.get('name'), 80)
                    if name:
                        return name
                stack.extend(v for v in obj.values() if isinstance(v, (dict, list)))
    tag = soup.select_one('meta[property="og:site_name"]')
    if tag and plain(tag.get('content'), 80):
        return plain(tag.get('content'), 80)
    for selector in ('header [itemprop="name"]', 'header .logo', 'header .brand', '.site-title'):
        tag = soup.select_one(selector)
        if tag and plain(tag.get_text(' ', strip=True), 80):
            return plain(tag.get_text(' ', strip=True), 80)
    if soup.title:
        title = plain(soup.title.get_text(' ', strip=True), 80)
        bits = re.split(r'\s+[|–—-]\s+', title)
        for bit in reversed(bits):
            if bit and bit.lower() not in ('menu', 'order online', 'home'):
                return bit
    return ''


def extract_environment_candidates(soup, url):
    """Favor visibly contextual café photography, not menu product images or logos."""
    found = []
    selectors = ('main img', 'article img', '.gallery img', '.hero img',
                 '.about img', '.interior img', '.store img', 'figure img')
    images = soup.select(', '.join(selectors))
    for img in images:
        alt = plain((img.get('alt') or '') + ' ' + (img.get('class') or [''])[0] if isinstance(img.get('class'), list) else (img.get('alt') or ''), 170).lower()
        parent = img.find_parent(['figure', 'section', 'article'])
        surrounding = plain(parent.get_text(' ', strip=True) if parent else '', 180).lower()
        context = alt + ' ' + surrounding
        positive = ('interior', 'coffeehouse', 'café', 'cafe', 'seating', 'storefront', 'patio', 'our space', 'inside', 'counter', 'our store', 'atmosphere')
        negative = ('latte', 'frappuccino', 'cookie', 'muffin', 'sandwich', 'menu item', 'logo', 'icon', 'badge')
        if not any(w in context for w in positive) or any(w in alt for w in negative):
            continue
        raw = img.get('data-src') or img.get('src') or ''
        if not raw:
            raw = (img.get('srcset') or '').split(',')[0].strip().split(' ')[0]
        candidate = urljoin(url, raw)
        if urlsplit(candidate).scheme in ('http', 'https') and candidate not in found:
            found.append(candidate)
    return found[:MAX_GALLERY * 3]


async def embed_image(client, url):
    """Best-effort public remote image fetch, validation and compression for portable HTML."""
    try:
        verified_url(url)
        async with client.stream('GET', url) as response:
            if response.status_code != 200 or not response.headers.get('content-type', '').lower().startswith('image/'):
                return ''
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks) > 3_000_000:
                    return ''
        with Image.open(BytesIO(chunks)) as source:
            if source.width < 160 or source.height < 120 or source.width * source.height > 25_000_000:
                return ''
            image = ImageOps.exif_transpose(source).convert('RGB')
            image.thumbnail((1150, 1150))
            stream = BytesIO()
            image.save(stream, format='JPEG', quality=75, optimize=True)
        return 'data:image/jpeg;base64,' + base64.b64encode(stream.getvalue()).decode('ascii')
    except Exception:
        return ''


def parse_page(html, url, items, navigation=None):
    soup = BeautifulSoup(html, 'html.parser')
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            parse_ld(json.loads(script.string or script.get_text()), items, url)
        except (ValueError, TypeError):
            pass
    context = menu_context(soup, url)
    crumbs = context.get('breadcrumb', [])
    category, subcategory = navigation_metadata(url, context, navigation or {})
    for ordinal, product in enumerate(element_products(soup,url,category)):
        add(items,product['category'],product['name'],product['description'],product['image'],product['sourceUrl'], subcategory=subcategory, product_order=ordinal)
        key=(product['category'].casefold(),subcategory.casefold(),normalize_name(product['name']).casefold())
        if key in items: items[key]['pageUrl'] = url

    return soup





SKIP_PATHS = ('/login', '/signin', '/register', '/account', '/checkout', '/cart',
              '/privacy', '/terms', '/legal', '/careers', '/gift', '/rewards',
              '/contact', '/locations', '/find-a-store', '/store-locator')
MENU_HINTS = ('menu', 'food', 'drink', 'beverage', 'coffee', 'tea', 'bakery',
              'breakfast', 'lunch', 'dinner', 'dessert', 'catering', 'product',
              'category', 'order', 'pdf')


def menu_context(soup, page_url):
    path = urlsplit(page_url).path.lower()
    breadcrumb = soup.select_one('[aria-label*="readcrumb"], .breadcrumb, nav.breadcrumb')
    bits = [plain(x.get_text(' ', strip=True), 70) for x in breadcrumb.select('a,li,span')] if breadcrumb else []
    heading = soup.select_one('main h1, [role="main"] h1, h1')
    title = plain(heading.get_text(' ', strip=True), 80) if heading else ''
    return {'path': path, 'breadcrumb': [x for x in bits if x], 'heading': title}


def candidate_links(soup, url, root, *, within_menu=False):
    """Return scored candidate URLs; include category names like 'Trending'."""
    results = {}
    current_path = urlsplit(url).path.rstrip('/').lower()
    main = soup.select_one('main, [role="main"], #main, .main-content')
    scope = main or soup
    page_is_menu = within_menu or any(x in current_path for x in MENU_HINTS)
    def consider(anchor, in_main):
        raw = anchor.get('href', '')
        if not raw or raw.startswith(('#', 'mailto:', 'tel:', 'javascript:')):
            return
        target = urldefrag(urljoin(url, raw))[0]
        parts = urlsplit(target)
        if parts.scheme not in ('https','http') or not same_site(target,root):
            return
        path = parts.path.lower().rstrip('/')
        if not path or path == current_path or any(x in path for x in SKIP_PATHS):
            return
        if re.search(r'\.(?:png|jpg|jpeg|gif|svg|webp|zip|mp4|css|js)$', path):
            return
        label = plain(anchor.get_text(' ', strip=True), 90).lower()
        image = anchor.find('img')
        if not label and image: label = plain(image.get('alt'), 90).lower()
        ancestor = anchor.find_parent(['nav','header','footer'])
        score = 0
        if any(word in path for word in MENU_HINTS): score += 5
        if any(word in label for word in MENU_HINTS): score += 3
        if in_main: score += 3
        if page_is_menu and in_main: score += 5
        if page_is_menu and (current_path + '/') in path: score += 8
        if anchor.find(['img','h2','h3','h4']): score += 2
        if ancestor: score -= 7
        if path.count('/') > 7: score -= 2
        if score >= (3 if page_is_menu else 5):
            results[target] = max(score, results.get(target,-100))
    for link in soup.select('a[href]'):
        consider(link, False)
    if main:
        for link in main.select('a[href]'):
            consider(link, True)
    return [target for target, score in sorted(results.items(), key=lambda kv: -kv[1])][:95]


def reject_navigation_as_product(items, navigation_urls, page_heading):
    """Discard navigation-only records without product-specific evidence."""
    labels = {normalize_name(x).casefold() for x in navigation_urls if x}
    for key, obj in list(items.items()):
        if obj['name'].casefold() in labels and not obj.get('description'):
            del items[key]


def category_labels(soup, base):
    labels = []
    for a in soup.select('main a[href], [role="main"] a[href]'):
        if a.find(['img','h2','h3','h4']):
            label = plain(a.get_text(' ', strip=True) or ((a.find('img') or {}).get('alt') if a.find('img') else ''),85)
            if label and label not in labels: labels.append(label)
    return labels


def pdf_as_evidence(data, page_url):
    try:
        reader = PdfReader(BytesIO(data))
        text = "\n".join((p.extract_text() or '') for p in reader.pages[:MAX_PDF_PAGES])[:15000]
        return {'url': page_url, 'title': 'PDF restaurant menu', 'text': text, 'blocks': [text[i:i+550] for i in range(0, min(len(text), 11000), 550)]}
    except Exception:
        return None


async def import_website(url, progress=None):
    def announce(stage, percent, message="", **numbers):
        if progress:
            progress(stage=stage, percent=percent, message=message, **numbers)
    announce("Discovering menu pages", 3, "Opening restaurant website")
    verified_url(url)
    root = site_root(url)
    items = OrderedDict()
    visited = set()
    pending = deque([(url, 0, False)])
    queued = {url}
    navigation = OrderedDict()  
    section_cards = set()  
    errors = []
    business_name = ''
    product_image_candidates = {}
    detail_links = {}
    gallery_candidates = []  
    page_evidences = []
    interactive_states_checked = 0
    interactive_pages = 0
    ai_attempts = 0
    ai_found = 0
    ai_errors = []
    rendered_count = 0
    pdf_count = 0
    async with httpx.AsyncClient(timeout=14.0, follow_redirects=False, headers=HEADERS, trust_env=False) as client:
        while pending and len(visited) < MAX_PAGES and len(items) < MAX_PRODUCTS:
            page, depth, in_menu = pending.popleft()
            if depth > 5 or page in visited or not same_site(page, root):
                continue
            verified_url(page)
            visited.add(page)
            announce("Discovering menu pages", min(48, 4 + int(44 * len(visited) / MAX_PAGES)),
                     "Checking menu pages", pagesChecked=len(visited), productsFound=len(items))
            try:
                resp = await client.get(page)
                if resp.status_code in (301, 302, 303, 307, 308):
                    dest = urljoin(page, resp.headers.get('location', ''))
                    if same_site(dest, root) and dest not in visited:
                        pending.appendleft((dest, depth, in_menu))
                        queued.add(dest)
                    continue
                if resp.status_code != 200:
                    errors.append(f'{urlsplit(page).path}: HTTP {resp.status_code}')
                    continue
                kind = resp.headers.get('content-type', '').lower()
                if 'pdf' in kind or urlsplit(page).path.lower().endswith('.pdf'):
                    if len(resp.content) <= MAX_PDF_BYTES:
                        evidence = pdf_as_evidence(resp.content, page)
                        if evidence and evidence['text'] and len(page_evidences) < MAX_AI_PAGES:
                            page_evidences.append(evidence)
                            pdf_count += 1
                    else:
                        errors.append('Skipped oversized PDF')
                    continue
                if 'html' not in kind or len(resp.content) > MAX_BYTES:
                    errors.append(f'{urlsplit(page).path}: unsupported or oversized page')
                    continue
                original_html = resp.text
                soup = parse_page(original_html, page, items, navigation)
                
                links = candidate_links(soup, page, root, within_menu=in_menu)
                html = original_html
                if rendered_count < MAX_BROWSER_PAGES:
                    try:
                        rendered = await render_page(page, root)
                        if rendered:
                            rendered_count += 1
                            html = rendered
                            soup = parse_page(html, page, items, navigation)
                            for link in candidate_links(soup, page, root, within_menu=in_menu):
                                if link not in links:
                                    links.append(link)
                    except Exception as exc:
                        errors.append(f'Browser rendering {urlsplit(page).path}: {type(exc).__name__}')
                product_image_candidates.update(collect_product_images(soup, page))
                detail_links.update(find_product_detail_links(soup, page, root))
                parent_label, _ = navigation_metadata(page, menu_context(soup, page), navigation)
                if page in navigation and navigation[page].get('label'):
                    parent_label = navigation[page]['label']
                for target, meta in navigation_cards(soup, page, root).items():
                    section_cards.add((plain(meta['label'],85).casefold(), _key(target)))
                    
                    if not meta['parent'] and page != url and parent_label and parent_label.casefold() not in ('menu', 'view menu', 'our menu', 'products'):
                        meta['parent'] = parent_label
                    merge_navigation(navigation, target, meta)
                
                
                
                if not any(_key(x.get('sourceUrl','')) == _key(page) for x in items.values()) and interactive_pages < 5:
                    try:
                        states = await explore_page(page, root, max_states=5)
                        interactive_pages += 1
                        for state in states[1:]:
                            interactive_states_checked += 1
                            state_soup = parse_page(state['html'], page, items, navigation)
                            product_image_candidates.update(collect_product_images(state_soup, page))
                            detail_links.update(find_product_detail_links(state_soup, page, root))
                            for target, meta in navigation_cards(state_soup, page, root).items():
                                section_cards.add((plain(meta['label'],85).casefold(), _key(target)))
                                merge_navigation(navigation, target, meta)
                            for target in candidate_links(state_soup, page, root, within_menu=True):
                                if target not in links: links.append(target)
                            if len(page_evidences) < MAX_AI_PAGES:
                                ev = page_evidence(state['html'], page)
                                ev['screenshot'] = state.get('screenshot', '')
                                ev['menu_context'] = menu_context(state_soup, page)
                                ev['navigation_labels'] = list(navigation_names(state_soup))
                                ev['hierarchy'] = navigation_metadata(page, ev['menu_context'], navigation)
                                page_evidences.append(ev)
                    except Exception as exc:
                        errors.append(f'Interactive inspection {urlsplit(page).path}: {type(exc).__name__}')
                if len(page_evidences) < MAX_AI_PAGES:
                    evidence = page_evidence(html, page)
                    evidence['menu_context'] = menu_context(soup, page)
                    evidence['navigation_labels'] = list(navigation_names(soup))
                    evidence['hierarchy'] = navigation_metadata(page, menu_context(soup, page), navigation)
                    page_evidences.append(evidence)
                if not business_name:
                    business_name = extract_business(soup, page)
                
                
                reject_navigation_as_product(items, category_labels(soup, page), menu_context(soup,page)['heading'])
                for link in links:
                    if link not in visited and link not in queued and depth < 5:
                        queued.add(link)
                        pending.append((link, depth + 1, True))
            except (httpx.HTTPError, UnicodeError) as ex:
                errors.append(f'{urlsplit(page).path}: {type(ex).__name__}')
            await asyncio.sleep(0.05)
    announce("AI analyzing menu", 51, "Identifying products and categories", pagesChecked=len(visited), productsFound=len(items))
    
    model_client = ai_client()
    if model_client and page_evidences:
        for evidence in page_evidences:
            ai_attempts += 1
            announce("AI analyzing menu", min(77, 51 + int(26 * ai_attempts / max(1, len(page_evidences)))),
                     "Analyzing page content", pagesChecked=len(visited), productsFound=len(items))
            try:
                parsed = await ai_extract(evidence, model_client)
                source = evidence['url']
                if not business_name and parsed.get('business_name'):
                    business_name = plain(parsed['business_name'], 80)
                for record in parsed.get('products', [])[:MAX_PRODUCTS]:
                    if not isinstance(record, dict):
                        continue
                    name = normalize_name(record.get('name'))
                    category = plain(record.get('category'), 70)
                    
                    if len(name) < 3 or name.casefold() not in evidence['text'].casefold():
                        continue
                    
                    if not category or (category.casefold() not in evidence['text'].casefold() and category.casefold() not in json.dumps(evidence.get('menu_context', {}), ensure_ascii=False).casefold()):
                        continue
                    if name.casefold() in {str(x).casefold() for x in evidence.get('navigation_labels', [])} and not record.get('description'):
                        continue
                    raw_image = record.get('image_url', '')
                    evidence_blob = json.dumps(evidence, ensure_ascii=False)
                    image = urljoin(source, raw_image) if raw_image and raw_image in evidence_blob else ''
                    if not plausible_image_url(image): image = ''
                    raw_product_url = record.get('product_url', '')
                    product_url = urljoin(source, raw_product_url) if raw_product_url and raw_product_url in evidence_blob else ''
                    if product_url and not same_site(product_url, root): product_url = ''
                    hierarchy = resolve_menu_hierarchy(source, navigation, url)
                    main = hierarchy[0] if hierarchy else (evidence['hierarchy'][0] if evidence.get('hierarchy') and evidence['hierarchy'][0] not in ('Menu','View Menu') else category)
                    sub = hierarchy[1] if hierarchy else (record.get('subcategory') or (evidence['hierarchy'][1] if evidence.get('hierarchy') else ''))
                    count_before = len(items)
                    add(items, main, name, record.get('description', ''), image, source, subcategory=sub)
                    key=(plain(main,70).casefold(),plain(sub,70).casefold(),name.casefold())
                    if key in items:
                        items[key]['pageUrl'] = source
                        if product_url: items[key]['productUrl'] = product_url
                    if len(items) > count_before:
                        ai_found += 1
                
            except Exception as exc:
                ai_errors.append(f'AI extraction on {urlsplit(evidence["url"]).path}: {type(exc).__name__}')
    elif not model_client:
        ai_errors.append('AI extraction skipped: configure OPENAI_API_KEY to enable it.')

    announce("Validating products", 80, "Removing navigation cards", pagesChecked=len(visited), productsFound=len(items))
    
    
    
    navigation_labels = {normalize_name(meta['label']).casefold() for meta in navigation.values()}
    
    
    
    known_section_paths = {urlsplit(target).path.rstrip('/') for target in navigation}
    known_section_names = set(navigation_labels)
    known_section_names.update(label for label, _ in section_cards)
    for ev in page_evidences:
        for label in ev.get('navigation_labels', []):
            if isinstance(label, str):
                known_section_names.add(normalize_name(label).casefold())
    removed_navigation = 0
    products = []
    for item in items.values():
        name = normalize_name(item['name'])
        source_path = urlsplit(item.get('sourceUrl', '')).path.lower()
        explicit_product = any(segment in source_path for segment in ('/product/', '/item/'))
        if not explicit_product and name.casefold() in known_section_names:
            removed_navigation += 1
            continue
        if not explicit_product and urlsplit(item.get('sourceUrl', '')).path.rstrip('/') in known_section_paths and name.casefold() in known_section_names:
            removed_navigation += 1
            continue
        item['name'] = name
        products.append(item)
    products, additional_navigation_removed = reconcile_menu_tree(products, navigation, url, section_cards)
    removed_navigation += additional_navigation_removed
    products = apply_original_order(products, navigation, url)
    descriptions_recovered = 0
    missing_details = []
    for product in products:
        if product.get('description'):
            continue
        detail_url = product.get('productUrl') or detail_links.get(normalize_name(product['name']).casefold())
        if detail_url and same_site(detail_url, root) and detail_url not in [x[0] for x in missing_details]:
            missing_details.append((detail_url, product))
    async with httpx.AsyncClient(timeout=12, follow_redirects=False, headers=HEADERS, trust_env=False) as detail_client:
        sem = asyncio.Semaphore(4)
        async def read_description(entry):
            detail_url, product = entry
            async with sem:
                try:
                    verified_url(detail_url)
                    res = await detail_client.get(detail_url)
                    if res.status_code != 200 or 'html' not in res.headers.get('content-type','').lower() or len(res.content) > MAX_BYTES:
                        return product, ''
                    return product, detail_description(res.text, product['name'])
                except (httpx.HTTPError, HTTPException, ValueError):
                    return product, ''
        for product, description in await asyncio.gather(*(read_description(x) for x in missing_details[:35])):
            if description and not product.get('description'):
                product['description'] = description
                descriptions_recovered += 1
    
    images_recovered = 0
    for product in products:
        if not product.get('image'):
            candidate = image_for_product(product_image_candidates, product['name'])
            if candidate:
                product['image'] = candidate
                images_recovered += 1
    announce("Loading product images", 84, "Checking source images", pagesChecked=len(visited), productsFound=len(products))
    
    image_urls = list(dict.fromkeys([x['image'] for x in products if x['image'] and plausible_image_url(x['image'])]))[:MAX_FETCH_IMAGES]
    embedded = {}
    semaphore = asyncio.Semaphore(5)
    async with httpx.AsyncClient(timeout=10, follow_redirects=False, headers=HEADERS, trust_env=False) as image_client:
        async def job(image_url):
            async with semaphore:
                return image_url, await embed_image(image_client, image_url)
        for image_url, content in await asyncio.gather(*(job(u) for u in image_urls)):
            if content:
                embedded[image_url] = content
    
    
    for product in products:
        product['image'] = embedded.get(product['image'], '') if product.get('image') else ''
    business_name = re.sub(r'^\s*menu\s*:\s*', '', business_name or '', flags=re.I).strip()
    gallery = []  
    extra = [f'Excluded {removed_navigation} category/navigation cards from the product list.'] if removed_navigation else []
    missing = sum(not product['image'] for product in products)
    remote_count = sum(bool(product['image']) and not product['image'].startswith('data:') for product in products)
    if remote_count:
        extra.append(f'{remote_count} product image(s) use source URLs and need an internet connection in exported HTML.')
    unresolved_sections = [meta['label'] for target, meta in navigation.items() if _key(target) not in {_key(x.get('sourceUrl','')) for x in products}]
    announce('Complete', 100, 'Imported menu ready', pagesChecked=len(visited), productsFound=len(products))
    if missing:
        extra.append(f'{missing} product image(s) could not be verified and were left blank.')
    return {'diagnostics': {'pagesChecked':len(visited), 'aiPagesAnalyzed':ai_attempts, 'aiProductsAdded':ai_found, 'productsFound':len(products), 'navigationCardsExcluded':removed_navigation, 'imagesEmbedded':len(embedded), 'descriptionsRecovered':descriptions_recovered, 'imageCandidatesFound':len(product_image_candidates), 'imageUrlsRecovered':images_recovered, 'missingProductImages':missing, 'menuSectionsDiscovered':len(navigation), 'sectionsWithoutVerifiedProducts':list(dict.fromkeys(unresolved_sections))[:40], 'browserPagesRendered':rendered_count, 'interactiveStatesChecked':interactive_states_checked, 'pdfMenusRead':pdf_count, 'aiErrors':ai_errors[:3]}, 'businessName': business_name, 'environmentImages': gallery, 'imagesImported': len(embedded),'sourceUrl': url, 'items': products, 'pagesChecked': len(visited),
            'warnings': (extra + [f'Found {len(products)} products on {len(visited)} pages. Import is best-effort, not guaranteed complete.'] if products else
                         [f'No verifiable products found. Checked {len(visited)} URL(s), rendered {rendered_count} page(s), read {pdf_count} PDF(s). The website may require a store selection, restrict access, or have no supported public menu content.']) + errors[:3] + ai_errors[:2]}
