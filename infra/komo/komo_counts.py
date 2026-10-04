import sqlite3, glob, sys
path = sys.argv[1] if len(sys.argv) > 1 else [x for x in glob.glob('/data/runtime/v3/d1/miniflare-D1DatabaseObject/*.sqlite') if 'metadata' not in x][0]
c = sqlite3.connect('file:' + path + '?mode=ro', uri=True)
print('integrity_check', c.execute('pragma integrity_check').fetchone()[0])
for (n,) in c.execute("select name from sqlite_master where type='table' order by 1").fetchall():
    print(n, c.execute('select count(*) from "%s"' % n).fetchone()[0])
