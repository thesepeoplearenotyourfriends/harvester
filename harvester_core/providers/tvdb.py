import urllib.parse
from .http import request_json
from ..storage import load_json, save_json_atomic

class TVDBClient:
    BASE="https://api4.thetvdb.com/v4"
    def __init__(self, api_key=None, pin=None, cache_file=None):
        if not api_key: raise ValueError("TVDB capability unavailable: set TVDB_API_KEY")
        self.api_key, self.pin, self.token = api_key, pin, None
        self.cache_file=cache_file; self.cache=load_json(cache_file,{}) if cache_file else {}
    def login(self):
        payload={"apikey":self.api_key}
        if self.pin: payload["pin"]=self.pin
        response=request_json(self.BASE+"/login", {"Content-Type":"application/json"}, payload)
        self.token=((response.get("data") or {}).get("token") or "").strip()
        if not self.token: raise RuntimeError("TVDB login returned no bearer token")
    def get(self,path,params=None):
        params=params or {}; key=path+"?"+urllib.parse.urlencode(sorted(params.items()))
        if key in self.cache: return self.cache[key], True
        if not self.token: self.login()
        response=request_json(self.BASE+path+"?"+urllib.parse.urlencode(params), {"Authorization":"Bearer "+self.token,"Accept":"application/json","User-Agent":"harvester/1"})
        data=response.get("data"); self.cache[key]=data
        if self.cache_file: save_json_atomic(self.cache_file,self.cache)
        return data, False
