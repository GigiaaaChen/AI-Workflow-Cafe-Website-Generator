"""Offline regression tests. Run: python test_v18.py"""
from collections import OrderedDict
from bs4 import BeautifulSoup
from menu_importer import (merge_navigation, menu_tree, reconcile_menu_tree, plausible_image_url, image_from, parse_page)
root='https://sample.example/menu'
nav=OrderedDict()
merge_navigation(nav,root+'/food/breakfast',dict(label='Breakfast',parent='',order=0))
merge_navigation(nav,root+'/food/breakfast',dict(label='Breakfast',parent='Food',order=1))
assert nav[root+'/food/breakfast']['parent']=='Food'
assert menu_tree(nav,root)[('sample.example','/menu/food/breakfast')]==('Food','Breakfast')
products=[dict(category='Breakfast',subcategory='',name='Egg Sandwich',description='Egg and bread',image='https://cdn.example/egg.jpg',sourceUrl=root+'/food/breakfast',pageUrl=root+'/food/breakfast'), dict(category='Food',subcategory='',name='Breakfast',description='',image='https://cdn.example/tile.jpg',sourceUrl=root+'/food/breakfast')]
final,removed=reconcile_menu_tree(products,nav,root)
assert removed==1 and len(final)==1
assert final[0]['category']=='Food' and final[0]['subcategory']=='Breakfast'
assert final[0]['image']=='https://cdn.example/egg.jpg'
assert not plausible_image_url('https://www.pret.com/en-US/products/US005029/iced-spiced-pumpkin-latte?category=Pumpkin')
assert plausible_image_url('https://images.ctfassets.net/asset/test.jpg')
assert image_from(BeautifulSoup('<a href="/products/123/latte"><img srcset="https://cdn.example/latte.jpg 1x, https://cdn.example/latte2.jpg 2x"></a>','html.parser').a,root)=='https://cdn.example/latte.jpg'
items=OrderedDict()
parse_page('<main><div class="menu-item"><h3>Latte</h3><img src="/latte.jpg"></div></main>',root,items)
assert len(items)==1
print('v18 offline regression tests passed')
