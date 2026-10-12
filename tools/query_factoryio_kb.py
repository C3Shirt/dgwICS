"""Search official source chapters or retrieve a compact component reference."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from factoryio_agents.component_kb import knowledge_base

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('query',nargs='?',default='')
    parser.add_argument('--prefab',action='append',default=[])
    parser.add_argument('--role',choices=('planner','scene','vision','plc'),default='vision')
    parser.add_argument('--limit',type=int,default=5)
    args=parser.parse_args();kb=knowledge_base()
    value=kb.references(args.prefab,args.role) if args.prefab else kb.search(args.query,args.limit) if args.query else kb.stats()
    print(json.dumps(value,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
