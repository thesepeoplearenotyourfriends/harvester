import urllib.parse
from .http import request_json
from ..storage import load_json, save_json_atomic

class TMDBClient:
    BASE = "https://api.themoviedb.org/3"
    def __init__(self, api_key=None, bearer_token=None, cache_file=None):
        if not api_key and not bearer_token:
            raise ValueError("TMDB capability unavailable: set TMDB_API_KEY or TMDB_BEARER_TOKEN")
        self.api_key, self.bearer_token = api_key, bearer_token
        self.cache_file = cache_file; self.cache = load_json(cache_file, {}) if cache_file else {}
    def get(self, path, params=None):
        params = {"language":"en-US", **(params or {})}
        key = path + "?" + urllib.parse.urlencode(sorted(params.items()), doseq=True)
        if key in self.cache: return self.cache[key]
        headers={"Accept":"application/json", "User-Agent":"harvester/1"}
        if self.bearer_token: headers["Authorization"]="Bearer " + self.bearer_token
        else: params["api_key"]=self.api_key
        result=request_json(self.BASE+path+"?"+urllib.parse.urlencode(params), headers)
        self.cache[key]=result
        if self.cache_file: save_json_atomic(self.cache_file, self.cache)
        return result
