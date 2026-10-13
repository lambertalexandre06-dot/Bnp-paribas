import urllib.request
req = urllib.request.Request('https://upload.wikimedia.org/wikipedia/fr/thumb/a/a3/Logo_BNP_Paribas.svg/100px-Logo_BNP_Paribas.svg.png', headers={'User-Agent': 'Mozilla/5.0'})
with urllib.request.urlopen(req) as response, open('static/logo.png', 'wb') as out_file:
    out_file.write(response.read())
