#!/usr/bin/env python3
"""Export inspectable offline HTML previews when a browser service is unavailable.

These are rendered Flask templates with synthetic data, not browser screenshots.
"""
import sys
import re
import tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from capture_ui import demo_app
from app import db
from app.models import User


def main():
    out=ROOT/'docs'/'ui-preview';out.mkdir(parents=True,exist_ok=True)
    css=(ROOT/'app'/'static'/'style.css').read_text()
    pages=[]
    with tempfile.TemporaryDirectory(prefix='.preview-',dir=ROOT) as directory:
        app,credentials,ids,day=demo_app(Path(directory))
        with app.test_client() as client:
            def save(name,title,path):
                response=client.get(path)
                assert response.status_code==200,(path,response.status_code)
                html=re.sub(r'<link rel="stylesheet" href="/static/style\.css(?:\?[^"]*)?">',
                            lambda match: '<style>'+css+'</style>', response.get_data(as_text=True))
                html=re.sub(r'(name="csrf_token" value=")[^"]*', r'\1', html)
                # Prevent offline forms/navigation from accidentally targeting another service.
                html=html.replace('</body>','<script>document.addEventListener("submit",function(e){e.preventDefault();});document.addEventListener("click",function(e){var a=e.target.closest("a");if(a && a.getAttribute("href").startsWith("/"))e.preventDefault();});</script></body>')
                (out/(name+'.html')).write_text(html)
                pages.append((name,title))
            save('login','参赛队长登录','/login')
            with client.session_transaction() as session:session['_user_id']=str(ids['captain'])
            for name,title,path in [('captain','队长工作台','/captain'),('booking','测试预约','/bookings/new'),('records','申请记录','/records'),('tools','工具借用','/tools/new'),('schedule','占用视图',f'/schedule?date={day}&view=week')]:save(name,title,path)
            with app.app_context():uid=User.query.filter_by(role='admin').one().id
            with client.session_transaction() as session:session['_user_id']=str(uid)
            save('reviewer','管理员工作台','/')
            with app.app_context():uid=User.query.filter_by(role='superadmin').one().id
            with client.session_transaction() as session:session['_user_id']=str(uid)
            for name,title,path in [('admin','总管理员工作台','/'),('review','申请审核','/review/'),('inventory','工具记录与库存','/tools/admin'),('people','人员信息管理','/people/'),('managers','管理员管理','/managers/'),('security','安全防护中心','/security/')]:save(name,title,path)
        with app.app_context():db.session.remove();db.engine.dispose()
    options=''.join(f'<option value="{name}">{title}</option>' for name,title in pages)
    index='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>界面离线预览</title><style>body{font:15px sans-serif;background:#e9eef5;color:#193354;margin:24px}h1{font-size:24px}select,button{font:inherit;padding:10px;margin:4px}iframe{border:1px solid #b9cadf;background:#fff;height:900px;display:block;margin-top:20px}p{line-height:1.8}.canvas{overflow:auto}</style><h1>桌面与手机界面离线预览</h1><p>由真实 Flask 模板与隔离演示数据生成。这是 HTML 预览，不是已验收的浏览器截图；表单不会提交。实际像素效果和操作流程仍需在可启动 Chromium 的环境运行 capture_ui.py 验证。</p><select id="page">OPTIONS</select><button onclick="resize(1280)">电脑 1280px</button><button onclick="resize(390)">手机 390px</button><button onclick="resize(320)">手机 320px</button><div class="canvas"><iframe id="preview" title="页面预览" src="login.html" width="1280"></iframe></div><script>var frame=document.getElementById('preview');document.getElementById('page').onchange=function(){frame.src=this.value+'.html'};function resize(width){frame.width=width;}</script></html>'''.replace('OPTIONS',options)
    (out/'index.html').write_text(index)
    print(f'Exported {len(pages)} offline HTML previews to docs/ui-preview/ (not screenshots).')
    return 0


if __name__=='__main__':raise SystemExit(main())
