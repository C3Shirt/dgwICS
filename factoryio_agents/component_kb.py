"""Shared official component facts and source chapters, with deterministic scoped retrieval."""
from copy import deepcopy
from functools import lru_cache
import hashlib
from pathlib import Path
import re

from .contracts import canonical_hash, file_hash, read_json, require

ROOT=Path(__file__).parent/'knowledge'


class KnowledgeBase:
    def __init__(self, directory=ROOT):
        directory=Path(directory)
        self.paths=[directory/name for name in ('official-documents.json','official-components.json')]
        self.corpus,self.index=[read_json(path) for path in self.paths]
        require(self.index['corpus_sha256']==canonical_hash(self.corpus),'official corpus identity mismatch')
        self.chapters={row['id']:row for row in self.corpus['chapters']}
        require(len(self.chapters)==len(self.corpus['chapters']),'duplicate official chapter IDs')
        for row in self.chapters.values():
            require(row['text_sha256']==hashlib.sha256(row['body'].encode()).hexdigest(),'official chapter content changed')
            require(row['url'].startswith('https://docs.factoryio.com/'),'non-official source URL')
        self.aliases={};self.by_chapter={};self.exact_titles={}
        for card in self.index['components']:
            require(card['function'] and all(k in self.chapters for k in card['chapter_ids']),'component source is missing')
            for prefab in card['prefabs']:
                require(prefab not in self.aliases,'ambiguous component prefab')
                self.aliases[prefab]=card
            for chapter in card['chapter_ids']:self.by_chapter.setdefault(chapter,[]).append(card)
            name=re.sub(r'[^a-z0-9]','',card['official_name'].casefold())
            self.exact_titles.setdefault(name,[]).append(card)
        self.identity=canonical_hash({'corpus':self.corpus,'index':self.index,'retrieval_version':1})
        self.file_hashes=[file_hash(path) for path in self.paths]

    def unchanged(self):
        require(all(file_hash(path)==digest for path,digest in zip(self.paths,self.file_hashes)),
                'official knowledge changed during this process; reload the framework before continuing')

    def lookup(self,prefab):
        value=self.aliases.get(prefab)
        if value is None:
            matches=self.exact_titles.get(re.sub(r'[^a-z0-9]','',prefab.casefold()),[])
            if len(matches)==1:value=matches[0]
        return deepcopy(value) if value else None

    def chapter(self,identifier):
        require(identifier in self.chapters,'unknown official chapter')
        return deepcopy(self.chapters[identifier])

    def stats(self):
        return {'source_count':len(self.corpus['sources']),'chapter_count':len(self.chapters),
                'component_family_count':len(self.index['components']),'prefab_count':len(self.aliases),
                'knowledge_sha256':self.identity}

    def references(self,prefabs,role):
        self.unchanged()
        requested=sorted(set(prefabs));grouped={};missing=[]
        for prefab in requested:
            card=self.lookup(prefab)
            if card is None:missing.append(prefab);continue
            item=grouped.setdefault(card['id'],{'component_id':card['id'],'prefabs':[],
                'official_name':card['official_name'],'function':card['function'],
                'function_basis':card.get('function_basis','official_text'),
                'sources':[{'chapter_id':key,'url':self.chapters[key]['url']} for key in card['chapter_ids']]})
            item['prefabs'].append(prefab)
            if role in ('scene','vision'):
                if card.get('appearance'):item['appearance']=deepcopy(card['appearance'])
                if card.get('markers'):item['markers']=deepcopy(card['markers'])
        return {'knowledge_sha256':self.identity,'reference_kind':'official_component_documentation',
                'requested_prefabs':requested,'components':list(grouped.values()),'missing_prefabs':missing}

    def search(self,query,limit=8):
        require(isinstance(query,str) and len(query)<=512,'invalid knowledge search')
        require(type(limit) is int and 1<=limit<=30,'invalid search limit')
        query=query.strip().casefold()
        if not query:return []
        tokens=re.findall(r'[a-z0-9_]+|[\u3400-\u9fff]+',query);rows=[]
        for chapter in self.chapters.values():
            cards=self.by_chapter.get(chapter['id'],[])
            aliases=' '.join(str(x) for c in cards for x in [c['official_name'],*c['prefabs'],*c['aliases']]).casefold()
            title=chapter['title'].casefold();body=chapter['body'].casefold()
            score=sum(5*(token in title)+8*(token in aliases)+min(3,body.count(token)) for token in tokens)
            if query in title or query in aliases:score+=20
            if not score:continue
            first=min((body.find(token) for token in tokens if token in body),default=0)
            start=max(0,first-120)
            rows.append({'chapter_id':chapter['id'],'title':chapter['title'],'url':chapter['url'],
                'score':score,'excerpt':chapter['body'][start:start+1400],
                'components':[deepcopy(c) for c in cards]})
        return sorted(rows,key=lambda r:(-r['score'],r['chapter_id']))[:limit]


@lru_cache(maxsize=1)
def knowledge_base():return KnowledgeBase()


def selected_prefabs(payload):
    for field in ('layout_review','task','task_spec','manifest','approved_layout','layout_reference','layout_proposal'):
        objects=(payload.get(field) or {}).get('objects',[])
        values={o.get('prefab') or o.get('type') for o in objects if isinstance(o,dict)}-{None,''}
        if values:return sorted(values)
    values={o.get('prefab') for o in (payload.get('io_contract') or {}).get('component_semantics',[])}-{None,''}
    if values:return sorted(values)
    catalog=payload.get('catalog') or {}
    return sorted({o.get('prefab') for o in catalog.get('prefabs',catalog.get('templates',[]))}-{None,''})


def references_for_payload(payload,role):
    return knowledge_base().references(selected_prefabs(payload),role)
