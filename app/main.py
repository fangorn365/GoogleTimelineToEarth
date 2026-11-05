
import sys, sqlite3, csv, math, traceback
from pathlib import Path
from datetime import datetime, timezone
from PySide6.QtCore import Qt, QDate, QTimer, QPointF
from PySide6.QtGui import QPainter, QColor, QPen, QFont
from PySide6.QtWidgets import *

def dt(ms): return datetime.fromtimestamp(ms/1000, timezone.utc)
def hav(a,b):
    R=6371000; p1,p2=map(math.radians,[a[0],b[0]])
    dp=math.radians(b[0]-a[0]); dl=math.radians(b[1]-a[1])
    h=math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*R*math.asin(min(1,math.sqrt(h)))

class MapView(QWidget):
    def __init__(self):
        super().__init__(); self.setMinimumSize(560,420); self.rows=[]; self.heat=False
        self.zoom=1.; self.pan=QPointF(); self.drag=None
    def set_rows(self, rows, heat):
        self.rows=rows; self.heat=heat; self.update()
    def mousePressEvent(self,e):
        if e.button()==Qt.LeftButton: self.drag=e.position()
    def mouseMoveEvent(self,e):
        if self.drag is not None:
            self.pan += e.position()-self.drag; self.drag=e.position(); self.update()
    def mouseReleaseEvent(self,e): self.drag=None
    def wheelEvent(self,e):
        self.zoom=max(.5,min(20,self.zoom*(1.15 if e.angleDelta().y()>0 else 1/1.15))); self.update()
    def paintEvent(self,e):
        p=QPainter(self); p.setRenderHint(QPainter.Antialiasing); p.fillRect(self.rect(),QColor("#101820"))
        w,h=self.width(),self.height()
        p.setPen(QPen(QColor("#263945"),1))
        for x in range(0,w,50): p.drawLine(x,0,x,h)
        for y in range(0,h,50): p.drawLine(0,y,w,y)
        if not self.rows:
            p.setPen(QColor("#e2e8ee")); p.drawText(self.rect(),Qt.AlignCenter,"Records.json öffnen, um den Verlauf zu laden."); return
        lats=[r[1] for r in self.rows]; lons=[r[2] for r in self.rows]
        a,b=min(lats),max(lats); c,d=min(lons),max(lons)
        if b-a<1e-5: a-=.001; b+=.001
        if d-c<1e-5: c-=.001; d+=.001
        def merc(v): return math.log(math.tan(math.pi/4+math.radians(max(-85,min(85,v)))/2))
        ya,yb=merc(a),merc(b); pad=35
        scale=min((w-2*pad)/(d-c),(h-2*pad)/(yb-ya))*self.zoom
        cx=w/2+self.pan.x(); cy=h/2+self.pan.y()
        def xy(lat,lon): return QPointF(cx+(lon-(c+d)/2)*scale,cy-(merc(lat)-(ya+yb)/2)*scale)
        if self.heat:
            p.setPen(Qt.NoPen); p.setBrush(QColor(255,90,35,24))
            step=max(1,len(self.rows)//5000)
            for r in self.rows[::step]: p.drawEllipse(xy(r[1],r[2]),12,12)
        p.setPen(QPen(QColor("#29b6f6"),1.5)); p.setBrush(Qt.NoBrush)
        prev=None
        for r in self.rows:
            q=xy(r[1],r[2])
            if prev is not None: p.drawLine(prev,q)
            prev=q
        p.setPen(Qt.NoPen); p.setBrush(QColor("#ffcc66"))
        for r in self.rows[::max(1,len(self.rows)//10000)]:
            p.drawEllipse(xy(r[1],r[2]),2,2)
        p.setPen(QColor("#e2e8ee")); p.setFont(QFont("Segoe UI",9))
        p.drawText(10,20,f"{len(self.rows):,} Punkte | Mausrad: Zoom | Ziehen: Verschieben".replace(",","."))

class App(QMainWindow):
    def __init__(self):
        super().__init__(); self.setWindowTitle("Google Timeline to Earth"); self.resize(1250,800)
        self.db=None; self.timer=QTimer(self); self.timer.timeout.connect(self.next_day); self.days=[]; self.idx=0
        root=QWidget(); self.setCentralWidget(root); v=QVBoxLayout(root)
        bar=QHBoxLayout()
        for label,fn in [("Records.json öffnen",self.open_file),("KML exportieren",self.kml),("GPX exportieren",self.gpx),("CSV exportieren",self.csv)]:
            b=QPushButton(label); b.clicked.connect(fn); bar.addWidget(b)
            if label!="Records.json öffnen": b.setEnabled(False); setattr(self,{"KML exportieren":"bk","GPX exportieren":"bg","CSV exportieren":"bc"}[label],b)
        bar.addStretch(); self.heat=QCheckBox("Heatmap"); self.heat.stateChanged.connect(self.refresh); bar.addWidget(self.heat)
        v.addLayout(bar)
        split=QSplitter(); self.map=MapView(); split.addWidget(self.map)
        side=QWidget(); s=QVBoxLayout(side); form=QFormLayout()
        self.start=QDateEdit(calendarPopup=True); self.end=QDateEdit(calendarPopup=True)
        for x in (self.start,self.end): x.setDisplayFormat("dd.MM.yyyy"); x.dateChanged.connect(self.refresh)
        form.addRow("Von",self.start); form.addRow("Bis",self.end); s.addLayout(form)
        self.stats=QLabel("Noch keine Datei geladen."); self.stats.setWordWrap(True); s.addWidget(self.stats)
        self.play=QPushButton("▶ Tages-Wiedergabe"); self.play.clicked.connect(self.toggle); s.addWidget(self.play)
        self.speed=QComboBox(); self.speed.addItems(["1 Tag/s","3 Tage/s","10 Tage/s"]); s.addWidget(self.speed)
        self.table=QTableWidget(0,3); self.table.setHorizontalHeaderLabels(["Tag","Punkte","Distanz*"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch); s.addWidget(QLabel("* geschätzte Luftlinien-Distanz")); s.addWidget(self.table,1)
        s.addWidget(QLabel("Lokale Track-Ansicht ohne Online-Kartenkacheln."))
        split.addWidget(side); split.setSizes([850,350]); v.addWidget(split,1)
        self.statusBar().showMessage("Standortdaten bleiben lokal auf diesem PC.")
    def open_file(self):
        path,_=QFileDialog.getOpenFileName(self,"Records.json öffnen","","JSON (*.json)")
        if not path:return
        try: import ijson
        except ImportError:
            r=QMessageBox.question(self,"Einmalige Installation","ijson wird benötigt. Einmalig aus dem Internet installieren?")
            if r!=QMessageBox.Yes:return
            import subprocess
            p=subprocess.run([sys.executable,"-m","pip","install","ijson"],capture_output=True,text=True)
            if p.returncode: QMessageBox.critical(self,"Fehler",p.stderr[-2000:]); return
            import ijson
        self.load(Path(path))
    def load(self,path):
        import ijson
        dbpath=path.with_name(path.stem+"_timeline.sqlite")
        if dbpath.exists(): dbpath.unlink()
        con=sqlite3.connect(str(dbpath)); con.execute("PRAGMA journal_mode=OFF"); con.execute("PRAGMA synchronous=OFF")
        con.execute("CREATE TABLE pts(ms INTEGER,lat REAL,lon REAL,acc REAL)")
        batch=[]; n=0
        try:
            with path.open("rb") as f:
                for r in ijson.items(f,"locations.item"):
                    try:
                        ms=int(r["timestampMs"]); lat=float(r["latitudeE7"])/1e7; lon=float(r["longitudeE7"])/1e7
                        acc=float(r.get("accuracy") or 0)
                        if -90<=lat<=90 and -180<=lon<=180: batch.append((ms,lat,lon,acc))
                    except (KeyError,ValueError,TypeError): continue
                    if len(batch)>=10000:
                        con.executemany("INSERT INTO pts VALUES(?,?,?,?)",batch); con.commit(); n+=len(batch); batch=[]
                        self.statusBar().showMessage(f"{n:,} Punkte importiert ...".replace(",",".")); QApplication.processEvents()
            if batch: con.executemany("INSERT INTO pts VALUES(?,?,?,?)",batch); con.commit(); n+=len(batch)
            if not n: raise ValueError("Keine gültigen GPS-Punkte im Feld locations gefunden.")
            con.execute("CREATE INDEX ix_pts ON pts(ms)"); con.commit()
            if self.db:self.db.close()
            self.db=con; self.dbpath=dbpath
            lo,hi=con.execute("SELECT MIN(ms),MAX(ms) FROM pts").fetchone()
            self.start.setDate(QDate.fromString(dt(lo).strftime("%Y-%m-%d"),"yyyy-MM-dd"))
            self.end.setDate(QDate.fromString(dt(hi).strftime("%Y-%m-%d"),"yyyy-MM-dd"))
            for b in (self.bk,self.bg,self.bc):b.setEnabled(True)
            self.refresh(); self.statusBar().showMessage(f"Import fertig: {n:,} GPS-Punkte".replace(",","."))
        except Exception as e:
            con.close(); QMessageBox.critical(self,"Importfehler",str(e)+"\\n\\n"+traceback.format_exc()[-1800:])
    def rows(self):
        if not self.db:return []
        a=self.start.date().toPython(); b=self.end.date().toPython()
        lo=int(datetime(a.year,a.month,a.day,tzinfo=timezone.utc).timestamp()*1000)
        hi=int(datetime(b.year,b.month,b.day,23,59,59,tzinfo=timezone.utc).timestamp()*1000)
        return self.db.execute("SELECT ms,lat,lon,acc FROM pts WHERE ms BETWEEN ? AND ? ORDER BY ms",(lo,hi)).fetchall()
    def refresh(self):
        if not self.db:return
        rows=self.rows(); self.map.set_rows(rows,self.heat.isChecked())
        if not rows:self.stats.setText("Keine Punkte im Zeitraum."); self.table.setRowCount(0); return
        days={}
        for r in rows:days.setdefault(dt(r[0]).date(),[]).append(r)
        dist=sum(hav((a[1],a[2]),(b[1],b[2])) for a,b in zip(rows,rows[1:]) if b[0]-a[0]<=1800000)
        self.stats.setText(f"{len(rows):,} GPS-Punkte\\n{len(days)} Tage\\n{dist/1000:.1f} km geschätzte Distanz\\n{dt(rows[0][0]).date()} bis {dt(rows[-1][0]).date()}".replace(",","."))
        self.table.setRowCount(len(days))
        for i,(day,pts) in enumerate(sorted(days.items())):
            d=sum(hav((a[1],a[2]),(b[1],b[2])) for a,b in zip(pts,pts[1:]) if b[0]-a[0]<=1800000)
            for j,x in enumerate([str(day),str(len(pts)),f"{d/1000:.1f} km"]):self.table.setItem(i,j,QTableWidgetItem(x))
    def kml(self):
        path,_=QFileDialog.getSaveFileName(self,"KML speichern","Standortverlauf.kml","KML (*.kml)")
        if not path:return
        rows=self.rows()
        with open(path,"w",encoding="utf-8") as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?><kml xmlns="http://www.opengis.net/kml/2.2" xmlns:gx="http://www.google.com/kml/ext/2.2"><Document><name>Google Timeline</name>')
            day=None
            for r in rows:
                d=dt(r[0]).date()
                if d!=day:
                    if day is not None:f.write("</gx:Track></Placemark>")
                    day=d; f.write(f"<Placemark><name>{day}</name><gx:Track><altitudeMode>clampToGround</altitudeMode>")
                f.write(f"<when>{dt(r[0]).strftime('%Y-%m-%dT%H:%M:%SZ')}</when><gx:coord>{r[2]} {r[1]} 0</gx:coord>")
            if day is not None:f.write("</gx:Track></Placemark>")
            f.write("</Document></kml>")
        QMessageBox.information(self,"Fertig","KML gespeichert. In Google Earth Pro über Datei → Öffnen laden.")
    def gpx(self):
        path,_=QFileDialog.getSaveFileName(self,"GPX speichern","Standortverlauf.gpx","GPX (*.gpx)")
        if not path:return
        with open(path,"w",encoding="utf-8") as f:
            f.write('<?xml version="1.0"?><gpx version="1.1" creator="Google Timeline to Earth" xmlns="http://www.topografix.com/GPX/1/1"><trk><name>Standortverlauf</name>')
            day=None
            for r in self.rows():
                d=dt(r[0]).date()
                if d!=day:
                    if day is not None:f.write("</trkseg>")
                    day=d; f.write("<trkseg>")
                f.write(f'<trkpt lat="{r[1]}" lon="{r[2]}"><time>{dt(r[0]).strftime("%Y-%m-%dT%H:%M:%SZ")}</time></trkpt>')
            if day is not None:f.write("</trkseg>")
            f.write("</trk></gpx>")
        QMessageBox.information(self,"Fertig","GPX exportiert.")
    def csv(self):
        path,_=QFileDialog.getSaveFileName(self,"CSV speichern","Standortverlauf.csv","CSV (*.csv)")
        if not path:return
        with open(path,"w",newline="",encoding="utf-8-sig") as f:
            w=csv.writer(f,delimiter=";"); w.writerow(["timestamp_utc","latitude","longitude","accuracy_m"])
            for r in self.rows():w.writerow([dt(r[0]).isoformat(),r[1],r[2],r[3]])
        QMessageBox.information(self,"Fertig","CSV exportiert.")
    def toggle(self):
        if self.timer.isActive():self.timer.stop();self.play.setText("▶ Tages-Wiedergabe");return
        if not self.db:return
        self.days=[x[0] for x in self.db.execute("SELECT DISTINCT date(ms/1000,'unixepoch') FROM pts ORDER BY 1")]
        self.idx=0;self.timer.start([1000,333,100][self.speed.currentIndex()]);self.play.setText("■ Stop")
    def next_day(self):
        if self.idx>=len(self.days):self.timer.stop();self.play.setText("▶ Tages-Wiedergabe");return
        d=QDate.fromString(self.days[self.idx],"yyyy-MM-dd");self.start.setDate(d);self.end.setDate(d);self.idx+=1

if __name__=="__main__":
    app=QApplication(sys.argv); app.setStyle("Fusion"); w=App(); w.show(); sys.exit(app.exec())
