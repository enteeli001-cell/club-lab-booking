"""Idempotent, editable internal examples. No material values embedded in models."""
def examples(material_id, section_id):
    def node(nid, x, y, z): return dict(id=str(nid), x=x, y=y, z=z)
    def member(mid, a, b, kind="frame"):
        return dict(id=str(mid), start=str(a), end=str(b), material=str(material_id), section=str(section_id), type=kind, roll=0)
    def support(nid, values): return dict(node=str(nid), **dict(zip(("ux","uy","uz","rx","ry","rz"), values)))
    def load(nid, fz, fx=0): return dict(node=str(nid), fx=fx, fy=0, fz=fz, mx=0, my=0, mz=0)
    beam = {"nodes": [node(1,0,0,0), node(2,.5,0,0), node(3,1,0,0)],
            "members": [member(1,1,2), member(2,2,3)],
            "supports": [support(1,[1,1,1,1,0,0]), support(3,[0,1,1,0,0,0])], "loads": [load(2,-1)], "displacement_limit": .005}
    bridge = {"nodes": [node(i+1,i*.25,0,0) for i in range(5)] + [node(i+6,i*.25+.125,0,.2) for i in range(4)],
              "members": [], "supports": [support(i,[int(i==1),1,int(i in (1,5)),0,0,0]) for i in range(1,10)],
              "loads": [load(3,-1)], "displacement_limit": .005}
    pairs = [(i,i+1) for i in range(1,5)] + [(i,i+1) for i in range(6,9)]
    pairs += [(i+1,i+6) for i in range(4)] + [(i+6,i+2) for i in range(4)]
    bridge["members"] = [member(i+1,*pair,"truss") for i,pair in enumerate(pairs)]
    pyramid = {"nodes": [node(1,-.25,-.25,0),node(2,.25,-.25,0),node(3,.25,.25,0),node(4,-.25,.25,0),node(5,0,0,.6)],
               "members": [member(i+1,*pair) for i,pair in enumerate([(1,2),(2,3),(3,4),(4,1),(1,3),(2,4),(1,5),(2,5),(3,5),(4,5)])],
               "supports": [support(i,[1]*6) for i in range(1,5)], "loads": [load(5,-10,2)], "displacement_limit": .005}
    grid = {"nodes": [], "members": [], "supports": [], "loads": [], "displacement_limit": .005}
    nid = lambda x,y,z: z*16+y*4+x+1
    pairs = set()
    def add(a,b): pairs.add(tuple(sorted((a,b))))
    for z in range(5):
        for y in range(4):
            for x in range(4):
                n=nid(x,y,z); grid["nodes"].append(node(n,x*.15,y*.15,z*.2))
                if z==0: grid["supports"].append(support(n,[1]*6))
                if z==4: grid["loads"].append(load(n,-.4,.1))
                if x<3: add(n,nid(x+1,y,z))
                if y<3: add(n,nid(x,y+1,z))
                if z<4: add(n,nid(x,y,z+1))
                if x<3 and y<3: add(n,nid(x+1,y+1,z))
                if x<3 and z<4: add(n,nid(x+1,y,z+1))
                if y<3 and z<4: add(n,nid(x,y+1,z+1))
    grid["members"] = [member(i+1,*pair) for i,pair in enumerate(sorted(pairs))]
    return [("beam", "简支梁：跨中集中荷载", "3 节点、2 梁单元；用于 PL³/48EI 校验。", beam),
            ("bridge", "平面桁架桥", "9 节点 Warren 桁架；面外位移已约束。", bridge),
            ("pyramid", "空间四棱锥塔架", "5 节点、10 梁单元；底座固定，顶部复合荷载。", pyramid),
            ("space_frame", "多层空间框架", "80 节点、325 梁单元；五层带斜撑空间框架。", grid)]
