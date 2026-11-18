"""Keep every role's menu directly accessible during navigation layout changes."""
from html.parser import HTMLParser

import pytest

from tests.conftest import login


class NavigationMarkup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_nav=False
        self.links=[]
        self.nested_menus=[]
        self.logout=None
        self.toggle=None
        self.stylesheet=None

    def handle_starttag(self,tag,attributes):
        attrs=dict(attributes)
        if tag=='link' and attrs.get('rel')=='stylesheet':self.stylesheet=attrs['href']
        if attrs.get('id')=='nav-toggle':self.toggle=attrs
        if tag=='nav' and attrs.get('id')=='site-nav':self.in_nav=True
        if not self.in_nav:return
        if tag=='a':self.links.append(attrs['href'])
        if tag in ('details','select','aside'):self.nested_menus.append(tag)
        if tag=='form':self.logout=attrs

    def handle_endtag(self,tag):
        if tag=='nav':self.in_nav=False


@pytest.mark.parametrize(('student_id','staff','expected','forbidden'),[
    ('2021001',False,
     {'/captain','/bookings/new','/bookings','/tools','/records','/schedule','/password'},
     {'/people/','/managers/','/review/'}),
    ('officer01',True,
     {'/','/tools/admin','/schedule','/review/','/mail/','/stats/','/audit/password-resets','/labs/'},
     {'/people/','/managers/','/security/','/activity/users','/activity/admins','/password'}),
    ('admin',True,
     {'/','/tools/admin','/schedule','/review/','/mail/','/stats/','/labs/','/security/','/people/',
      '/managers/','/activity/users','/activity/admins','/users/','/duties/','/tools/catalog','/password'},
     {'/bookings/new','/captain'}),
])
def test_roles_keep_direct_navigation_links(client,seed,student_id,staff,expected,forbidden):
    login(client,student_id,staff=staff)
    response=client.get('/captain' if not staff else '/')
    assert response.status_code==200
    nav=NavigationMarkup();nav.feed(response.get_data(as_text=True))
    assert expected<=set(nav.links)
    assert not forbidden.intersection(nav.links)
    assert not nav.nested_menus
    assert nav.logout['action']=='/logout' and nav.logout['method']=='post'
    assert nav.toggle['aria-controls']=='site-nav' and nav.toggle['aria-expanded']=='false'
    # The template requests a new stylesheet URL instead of reusing cached sidebar CSS.
    assert '?v=topnav-' in nav.stylesheet
    assert client.get(nav.stylesheet).status_code==200
