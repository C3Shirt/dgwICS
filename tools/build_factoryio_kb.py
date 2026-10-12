"""Import the installed official manual into shared, versioned chapter and component indexes."""
import argparse
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sys
from urllib.parse import urljoin

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from factoryio_agents.contracts import canonical_hash, require, write_json

PAGES=('items','heavy-load','light-load','sensors','operators','stations','warning-devices','walkways','emitter','remover')
# Explicit mappings to documented component families; there is no fuzzy type substitution.
MAPPINGS={
 'items#stackable-box':(['StackableBox'],['可堆叠箱']),
 'heavy-load#roller-conveyor':(['RollerConveyor2M','RollerConveyor4M','RollerConveyor6M'],['辊筒输送带']),
 'heavy-load#turntable':(['Turntable'],['转台']),
 'heavy-load#chute-conveyor-low':(['ChuteConveyorLow'],['低位滑槽']),
 'light-load#belt-conveyors':(['BeltConveyor2M','BeltConveyor4M'],['皮带输送带','传送带']),
 'light-load#conveyor-scale':(['ConveyorScale'],['输送带称重秤']),
 'light-load#pop-up-wheel-sorter':(['PopUpWheelSorter'],['顶升轮式分拣器']),
 'light-load#aligners':(['Aligner3'],['对齐器']),
 'light-load#bracket-and-metal-corner':(['MetalCorner'],['金属角件']),
 'sensors#diffuse-sensor':(['DiffusePhotoelectricSensor'],['漫反射光电传感器','光电传感器','蓝色光电传感器']),
 'sensors#retroreflective-sensor-and-reflector':(['RetroreflectivePhotoelectricSensor','RetroreflectiveMirror'],['反射式光电传感器','反射板']),
 'sensors#light-array-emitter-and-receiver':(['LightCurtainEmitter','LightCurtainReceiver'],['光幕','光幕发射器','光幕接收器']),
 'operators#emergency-stop':(['EmergencyStopButton'],['急停按钮']),
 'operators#push-buttons':(['StartPushButton','StopPushButton','ResetPushButton'],['按钮','启动按钮','停止按钮','复位按钮']),
 'operators#digital-display':(['DigitalDisplay'],['数字显示器']),
 'operators#selector':(['Selector'],['选择开关']),
 'operators#electric-switchboard':(['ElectricSwitchboard'],['控制面板']),
 'operators#column':(['Column'],['立柱']),
 'warning-devices#stack-light':(['VisualStatusIndicator'],['状态指示灯','三色灯']),
 'walkways#safeguard':(['Safeguard','SafeguardI','SafeguardL'],['防护栏']),
 'emitter#emitter':(['Emitter'],['供料器','发射器']),
 'remover#remover':(['Remover'],['移除器'])}


class Text(HTMLParser):
    def __init__(self):
        super().__init__();self.parts=[];self.paragraphs=[];self.images=[];self.current=[];self.in_p=0;self.skip=0
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag in ('script','style'):self.skip+=1
        if tag=='p':self.in_p+=1;self.current=[]
        if tag in ('p','li','tr','h1','h2','h3','h4','h5'):self.parts.append('\n')
        if tag in ('td','th'):self.parts.append(' | ')
        if tag=='img' and attrs.get('src'):self.images.append({'alt':attrs.get('alt',''),'path':attrs['src']})
    def handle_endtag(self,tag):
        if tag in ('script','style'):self.skip=max(0,self.skip-1)
        if tag=='p':
            value=' '.join(''.join(self.current).split())
            if value:self.paragraphs.append(value)
            self.in_p=max(0,self.in_p-1)
        if tag in ('p','li','tr','h1','h2','h3','h4','h5'):self.parts.append('\n')
    def handle_data(self,data):
        if self.skip:return
        data=data.replace('¶','')
        self.parts.append(data)
        if self.in_p:self.current.append(data)
    def value(self):
        return '\n'.join(' '.join(line.split()) for line in ''.join(self.parts).splitlines() if line.strip())


def import_manual(doc_root, output, visual_reference=None):
    doc_root=Path(doc_root).resolve();output=Path(output)
    sources=[];chapters=[]
    for name in PAGES:
        path=doc_root/'manual/parts'/(name+'.html');require(path.is_file(),'missing official page '+str(path))
        raw=path.read_bytes();html=raw.decode('utf-8-sig')
        start=html.find('<h1');end=html.find('</article>',start)
        require(start>=0 and end>start,'official page has no article body')
        body=html[start:end];url='https://docs.factoryio.com/manual/parts/'+name+'/'
        sources.append({'id':'parts:'+name,'url':url,'source_path':str(path),
                        'sha256':hashlib.sha256(raw).hexdigest(),'basis':'installed_official_documentation'})
        headings=list(re.finditer(r'<h([12])\b[^>]*\bid="([^"]+)"[^>]*>(.*?)</h\1>',body,re.S))
        for n,heading in enumerate(headings):
            segment=body[heading.start():headings[n+1].start() if n+1<len(headings) else len(body)]
            parser=Text();parser.feed(segment)
            title=Text();title.feed(heading.group(3))
            identifier='parts:'+name+'#'+heading.group(2)
            value=parser.value()
            chapters.append({'id':identifier,'source_id':'parts:'+name,'title':title.value(),'heading_level':int(heading.group(1)),
                'url':url+'#'+heading.group(2),'body':value,'text_sha256':hashlib.sha256(value.encode()).hexdigest(),
                'intro':parser.paragraphs[0] if parser.paragraphs else '',
                'images':[{'alt':i['alt'],'url':urljoin('https://docs.factoryio.com/manual/parts/',i['path'])}
                          for i in parser.images]})
    by_id={row['id']:row for row in chapters};cards=[]
    curated={row['prefab']:row for row in (visual_reference or {}).get('components',[])}
    for key,(prefabs,aliases) in MAPPINGS.items():
        identifier='parts:'+key;chapter=by_id[identifier]
        card={'id':identifier,'official_name':chapter['title'],'prefabs':prefabs,'aliases':aliases,
              'function':chapter['intro'],'function_basis':'official_text','chapter_ids':[identifier]}
        candidates=[curated[p] for p in prefabs if p in curated]
        if candidates:
            extra=candidates[0];card['function']=extra['official_function_en'];card['function_basis']='curated_summary_of_official_text'
            if extra.get('appearance'):
                card['appearance']={'text':extra['appearance']['description_en'],'basis':extra['appearance']['basis']}
            card['markers']=[{'text':m['description_en'],'basis':m['basis']} for m in extra.get('visual_markers',[])]
        require(bool(card['function']),'component has no official functional description: '+key)
        cards.append(card)
    mapped={c['id'] for c in cards}
    for chapter in chapters:
        if chapter['heading_level']!=2 or chapter['id'] in mapped or not chapter['intro'] or chapter['title'] in ('Options','Configurations'):
            continue
        cards.append({'id':chapter['id'],'official_name':chapter['title'],'prefabs':[],'aliases':[],
                      'function':chapter['intro'],'function_basis':'official_text','chapter_ids':[chapter['id']]})
    # A flow-direction cue is explicitly described by the official creation tutorial.
    belt=next(c for c in cards if 'BeltConveyor4M' in c['prefabs'])
    belt['markers']=[{'text':'The white arrow indicates the conveying flow direction.','basis':'official_text',
        'source_url':'https://docs.factoryio.com/getting-started/creating-a-scene/'}]
    diffuse=next(c for c in cards if 'DiffusePhotoelectricSensor' in c['prefabs'])
    diffuse.setdefault('markers',[]).insert(0,{'text':'The red LED indicates detection of a solid object; the range line is a separate visualization.',
                                             'basis':'official_text'})
    for marker in diffuse['markers']:
        if marker['basis']=='text_and_image_interpretation':
            marker['text']='Range-line color alone is not a verified detection-state rule; the red-versus-white line transition has not been established.'
    corpus={'schema_version':1,'software':'Factory I/O','source_kind':'installed_official_documentation',
            'sources':sources,'chapters':chapters}
    index={'schema_version':1,'corpus_sha256':canonical_hash(corpus),'components':cards,
           'retrieval_policy':'Exact prefab lookup, deduplicated component families, role-specific compact views.'}
    write_json(output/'official-documents.json',corpus);write_json(output/'official-components.json',index)
    return {'sources':len(sources),'chapters':len(chapters),'component_families':len(cards),
            'prefab_aliases':sum(len(c['prefabs']) for c in cards),'corpus_sha256':index['corpus_sha256']}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--doc-root',type=Path,required=True)
    parser.add_argument('--out',type=Path,default=ROOT/'factoryio_agents/knowledge')
    parser.add_argument('--visual-reference',type=Path)
    args=parser.parse_args()
    visual=json.loads(args.visual_reference.read_text(encoding='utf-8')) if args.visual_reference else None
    print(json.dumps(import_manual(args.doc_root,args.out,visual),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
