"""Offline smoke tests for deterministic parsing and AI evidence extraction."""
from collections import OrderedDict
from ai_extractor import page_evidence
from menu_importer import parse_page

html = '''<html><head><title>Sample Coffee House | Menu</title></head><body>
<section><h2>Hot Coffee</h2><div class="menu-item"><h3>Maple Latte</h3><p>Espresso and maple syrup</p>
<img src="/maple.jpg" alt="Maple Latte"></div></section></body></html>'''
items = OrderedDict()
soup = parse_page(html, 'https://example.com/menu', items)
assert any(i['name'] == 'Maple Latte' for i in items.values())
assert 'Maple Latte' in page_evidence(html, 'https://example.com/menu')['text']
print('Offline parser/evidence smoke tests passed')

from menu_importer import candidate_links, category_labels, reject_navigation_as_product
from bs4 import BeautifulSoup
flat = BeautifulSoup('<main><div class="menu-item"><h3>Latte</h3></div></main>', 'html.parser')
assert candidate_links(flat, 'https://example.com/menu', 'example.com') == []
nested = BeautifulSoup('<main><a href="/menu/trending"><img src="x.jpg" alt="Trending"><h2>Trending</h2></a><a href="/menu/trending/cookie"><h3>Cookie</h3></a></main>', 'html.parser')
links = candidate_links(nested, 'https://example.com/menu', 'example.com')
assert 'https://example.com/menu/trending' in links
assert 'https://example.com/menu/trending/cookie' in links
print('Flat and nested discovery tests passed')

from menu_importer import navigation_cards, apply_original_order, navigation_metadata
root='https://example.com/menu'
markup=BeautifulSoup('<main><h2>Featured</h2><a href="/menu/trending"><img src="a.jpg"><h3>Trending</h3></a><a href="/menu/special"><h3>Specials</h3></a><h2>Drinks</h2><a href="/menu/coffee"><h3>Coffee</h3></a></main>','html.parser')
nav=navigation_cards(markup, root, 'example.com')
assert nav['https://example.com/menu/trending']['parent']=='Featured'
assert nav['https://example.com/menu/coffee']['parent']=='Drinks'
items=[{'category':'Drinks','subcategory':'Coffee','name':'Latte','productOrder':0}, {'category':'Featured','subcategory':'Trending','name':'Cookie','productOrder':0}]
ordered=apply_original_order(items,nav,root)
assert [x['name'] for x in ordered]==['Cookie','Latte']
assert navigation_metadata('https://example.com/menu/trending',{'breadcrumb':[], 'heading':'Trending'},nav)==('Featured','Trending')
print('Ordering and hierarchy tests passed')
