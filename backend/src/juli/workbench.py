"""Package-owned static workbench; protected data stays behind bearer-auth APIs."""
from importlib.resources import files
from fastapi.responses import Response


def attach_workbench(app):
    @app.get('/', include_in_schema=False)
    def index():
        return asset('index.html')

    @app.get('/workbench/{name}', include_in_schema=False)
    def asset(name: str):
        types = {'index.html':'text/html', 'app.js':'text/javascript', 'style.css':'text/css'}
        if name not in types:
            return Response(status_code=404)
        content = files('juli').joinpath('web', name).read_bytes()
        return Response(content, media_type=types[name], headers={
            'Cache-Control':'no-store', 'X-Content-Type-Options':'nosniff',
            'Referrer-Policy':'no-referrer',
            'Content-Security-Policy':"default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"})
