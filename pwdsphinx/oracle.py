#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2018-2021, Marsiske Stefan
# SPDX-License-Identifier: GPL-3.0-or-later

import socket, sys, ssl, os, datetime, binascii, shutil, os.path, traceback, struct
import pysodium
import equihash
import pyoprf
from pyoprf import noisexk
from binascii import a2b_base64
from pwdsphinx.config import getcfg
from pwdsphinx.consts import *
from pwdsphinx.utils import split_by_n
cfg = getcfg('sphinx')

verbose = cfg['server'].get('verbose', False)
address = cfg['server'].get('address', '127.0.0.1')
port = int(cfg['server'].get('port',2355))
timeout = int(cfg['server'].get('timeout',"3"))
max_kids = int(cfg['server'].get('max_kids',5))
datadir = os.path.expanduser(cfg['server'].get('datadir',"/var/lib/sphinx"))
noisekey = os.path.expanduser(cfg['server']['noisekey'])
authorized_keys = os.path.expanduser(cfg['server']['authorized_keys'])
try:
    ssl_key = os.path.expanduser(cfg['server']['ssl_key'])
except KeyError:
    print("Error: ssl_key missing! must specify it in the config file")

try:
    ssl_cert = os.path.expanduser(cfg['server']['ssl_cert'])
except KeyError:
    print("Error: ssl_cert missing! must specify it in the config file")

rl_decay = int(cfg['server'].get('rl_decay',1800))
rl_threshold = int(cfg['server'].get('rl_threshold',1))
rl_gracetime = int(cfg['server'].get('rl_gracetime',10))

if(verbose):
  print(f"address:      {address}:{port}")
  print(f"timeout:      {timeout}s")
  print(f"max kids:     {max_kids}")
  print(f"datadir:      {datadir}")
  print(f"noisekey:     {noisekey}")
  print(f"ssl_key:      {ssl_key}")
  if 'ssl_cert' in globals():
      print(f"ssl_cert:     {ssl_cert}")
  print(f"rl decay:     {rl_decay}")
  print(f"rl threshold: {rl_threshold}")
  print(f"rl gracetime: {rl_gracetime}")

Difficulties = [
    # timeouts are based on benchmarking a raspberry pi 1b
    { 'n': 60,  'k': 4, 'timeout': 1 },    # 320KiB, ~0.02
    { 'n': 65,  'k': 4, 'timeout': 2 },    # 640KiB, ~0.04
    { 'n': 70,  'k': 4, 'timeout': 4 },    # 1MiB, ~0.08
    { 'n': 75,  'k': 4, 'timeout': 9 },    # 2MiB, ~0.2
    { 'n': 80,  'k': 4, 'timeout': 16 },   # 5MiB, ~0.5
    { 'n': 85,  'k': 4, 'timeout': 32 },   # 10MiB, ~0.9
    { 'n': 90,  'k': 4, 'timeout': 80 },   # 20MiB, ~2.4
    { 'n': 95,  'k': 4, 'timeout': 160 },  # 40MiB, ~4.6
    # timeouts below are interpolated from above
    { 'n': 100, 'k': 4, 'timeout': 320 },  # 80MiB, ~7.8
    { 'n': 105, 'k': 4, 'timeout': 640 },  # 160MiB, ~25
    { 'n': 110, 'k': 4, 'timeout': 1280 }, # 320MiB, ~57
    { 'n': 115, 'k': 4, 'timeout': 2560 }, # 640MiB, ~70
    { 'n': 120, 'k': 4, 'timeout': 5120 }, # 1GiB, ~109
]
RL_Timeouts = {(e['n'],e['k']): e['timeout'] for e in Difficulties}

normal = "\033[38;5;%sm"
reset = "\033[0m"

def fail(s):
    if verbose:
        traceback.print_stack()
        print('fail')
    s.send(b'\x00\x04fail') # plaintext :/
    s.shutdown(socket.SHUT_RDWR)
    s.close()
    os._exit(0)

def pop(obj, cnt, astype=None):
  if astype is not None:
      return astype(obj[:cnt]), obj[cnt:]
  return obj[:cnt], obj[cnt:]

def verify_blob(msg, pk):
  sig = msg[-64:]
  msg = msg[:-64]
  pysodium.crypto_sign_verify_detached(sig, msg, pk)
  return msg

def save_blob(path,fname,blob):
  path = os.path.join(datadir, path, fname)
  with open(path,'wb') as fd:
    os.fchmod(fd.fileno(),0o600)
    fd.write(blob)

def read_pkt(s,size):
    res = []
    read = 0
    while read<size:
      res.append(s.recv(size-read))
      read+=len(res[-1])
    return b''.join(res)

def load_authkeys(path):
    res = []
    with open(path, 'r') as fd:
        for line in fd:
            b64key, name = line.split(' ', 1)
            name = name.strip()
            key = a2b_base64(b64key)
            res.append((key,name))
    return res

def setup_noise_sessions(s, n, privkey, auth_keys):
    # sets up a noise session with all peer
    # send own pubkey
    pk = noisexk.pubkey(privkey)
    s.sendall(pk)

    # get all peers pubkeys
    pubkeys = split_by_n(read_pkt(s,32*n),32)
    if len(pubkeys) != len(set(pubkeys)):
        print(f"invalid number of distinct noisekeys ({len(set(pubkeys))} != {n}(n)) ")
        fail(s)

    sender_sessions = []
    msgs = []

    # initiate sessions with all peers
    for pubkey in pubkeys:
        session, msg = noisexk.initiator_session(privkey, bytes(pubkey))
        sender_sessions.append(session)
        msgs.append(msg)
    s.sendall(b''.join(msgs))

    # respond to all session requests from peers
    receiver_sessions = []
    msg1s = read_pkt(s,48*n)
    msgs = []

    for msg in split_by_n(msg1s, 48):
        session, msg = noisexk.responder_session(privkey, auth_keys, msg)
        receiver_sessions.append(session)
        msgs.append(msg)
    s.sendall(b''.join(msgs))

    # receive final handshake message from responder
    msg2s = read_pkt(s,48*n)
    msgs = []
    for msg, session in zip(split_by_n(msg2s, 48), sender_sessions):
        noisexk.initiator_session_complete(session, msg)

    return sender_sessions, receiver_sessions

def update_blob(s):
    signed_id = s.recv(32+64)
    if len(signed_id)!=32+64:
      fail(s)
    if sum(signed_id[:32]) == 0: return

    id = binascii.hexlify(signed_id[:32]).decode()
    pk = load_blob(id,'pub')
    if pk is None:
      if os.path.exists(os.path.join(datadir,id)):
        print("user blob authkey not found, but dir exists:", id)
        fail(s)
      new = True
      blob = b'\x00\x00'
    else:
      try:
        blob = verify_blob(signed_id,pk)
      except ValueError:
        print('invalid signature on user blob id')
        fail(s)
      blob = load_blob(id,'blob')
      if blob is None:
        print("user blob authkey fund, but no blob for id:", id)
        fail(s)
      new = False
    s.sendall(blob)
    if new:
      pk = s.recv(32)
      prefix = s.recv(2)
      bsize = struct.unpack('!H', prefix)[0]
      signedblob = read_pkt(s, bsize+64)
      blob = pk+prefix+signedblob
      try:
        blob = verify_blob(blob,pk)
      except ValueError:
        print('invalid signature on msg')
        fail(s)
      blob = blob[32:]
      # create directories
      if not os.path.exists(datadir):
        os.mkdir(datadir,0o700)
      tdir = os.path.join(datadir,id)
      if not os.path.exists(tdir):
        os.mkdir(tdir,0o700)
      # save pubkey
      save_blob(id,'pub',pk)
    else:
      prefix = s.recv(2)
      bsize = struct.unpack('!H', prefix)[0]
      signedblob = read_pkt(s, bsize+64)
      blob = prefix+signedblob
      pk = load_blob(id,'pub')
      try:
        blob = verify_blob(blob,pk)
      except ValueError:
        print('invalid signature on msg')
        fail(s)
    save_blob(id,'blob',blob)

# msg format: 0x00|id[32]|alpha[32]
def create(s, msg):
    if len(msg)!=65:
      fail(s)
    if verbose: print('Data received:',msg.hex())
    op,   msg = pop(msg,1)
    id,   msg = pop(msg,32)
    alpha,msg = pop(msg,32)

    # check if id is unique
    id = binascii.hexlify(id).decode()
    tdir = os.path.join(datadir,id)
    if(os.path.exists(tdir)):
      fail(s)

    # 1st step OPRF with a new seed
    k=pysodium.randombytes(32)
    try:
      beta = pyoprf.evaluate(k, alpha)
    except:
      fail(s)

    s.send(beta)

    # wait for auth signing pubkey and rules
    msg = s.recv(32+RULE_SIZE+64) # pubkey, rule, signature
    if len(msg)!=32+RULE_SIZE+64:
      fail(s)
    # verify auth sig on packet
    pk = msg[0:32]
    try:
      msg = verify_blob(msg,pk)
    except ValueError:
      fail(s)

    rules = msg[32:]

    # 3rd phase
    update_blob(s) # add user to host record

    if not os.path.exists(datadir):
        os.mkdir(datadir,0o700)
    os.mkdir(tdir,0o700)

    save_blob(id,'key',k)
    save_blob(id,'pub',pk)
    save_blob(id,'rules',rules)

    s.send(b'ok')

def dkg(s, n, t, index, aux):
    with open(noisekey, 'rb') as fd:
        privkey = fd.read()
    if len(privkey) != 32:
        print("Invalid noisekey")
        fail(s)
    auth_keys = load_authkeys(authorized_keys)

    tx, rx = setup_noise_sessions(s, n, privkey, auth_keys)

    ## 1st step OPRF with a new seed
    # perform dkg to collectively generate new seed
    pk, sk = pysodium.crypto_sign_keypair()
    s.send(pk)
    pks = read_pkt(s,len(pk)*n)

    c_hash, signed_c, shares, transcript = pyoprf.dkg_start(n,t,sk)
    pysodium.crypto_generichash_update(transcript, aux)

    s.send(c_hash)
    c_hashes= read_pkt(s, len(c_hash)*n)

    s.send(signed_c)
    signed_commitments= read_pkt(s, len(signed_c)*n)

    s.send(b''.join(noisexk.send_msg(session, share) for share,session in zip(shares,tx)))

    msg = read_pkt(s, n*(33+64))

    shares = []
    for ct,session in zip(split_by_n(msg,33+64), rx):
        shares.append(noisexk.read_msg(session, ct))

    complaints, transcript = pyoprf.dkg_verify_commitments(n,t,index,c_hashes,signed_commitments,pks,shares,transcript)

    s.send(struct.pack("B", len(complaints))+complaints)
    # todo handle complaints by recovering from recoverable
    # inconsistencies.

    share, final_msg = pyoprf.dkg_finish(n, shares, index, sk, transcript)

    return share

# msg format: 0xf0|index|threshold|n|id[32]|alpha[32]]
def create_dkg(s, msg):
    if len(msg)!=68:
      fail(s)
    if verbose: print('Data received:',msg.hex())
    op,    msg = pop(msg,1)
    index, msg = pop(msg,1,lambda x: x[0])
    t,     msg = pop(msg,1,lambda x: x[0])
    n,     msg = pop(msg,1,lambda x: x[0])
    id,    msg = pop(msg,32)
    alpha, msg = pop(msg,32)
    aux = b'%s%s' % (op, alpha) # for the transcript

    # check if id is unique
    id = binascii.hexlify(id).decode()
    tdir = os.path.join(datadir,id)
    if(os.path.exists(tdir)):
      fail(s)

    xi = dkg(s,n,t,index, aux)

    #k=pysodium.randombytes(32)
    try:
      beta = pyoprf.evaluate(xi[1:], alpha)
    except:
      fail(s)

    msg = bytes([xi[0]])+beta
    s.send(msg)

    # wait for auth signing pubkey and rules
    msg = s.recv(32+RULE_SIZE+64) # pubkey, rule, signature
    if len(msg)!=32+RULE_SIZE+64:
      fail(s)
    # verify auth sig on packet
    pk = msg[:32]
    try:
      msg = verify_blob(msg,pk)
    except ValueError:
      fail(s)

    rules = msg[32:]

    # 3rd phase
    update_blob(s) # add user to host record

    if not os.path.exists(datadir):
        os.mkdir(datadir,0o700)
    os.mkdir(tdir,0o700)

    save_blob(id,'key',xi)
    save_blob(id,'pub',pk)
    save_blob(id,'rules',rules)

    s.send(b'ok')

def load_blob(path,fname,size=None):
    f = os.path.join(datadir,path,fname)
    if not os.path.exists(f):
        if verbose: print('%s does not exist' % f)
        return
    with open(f,'rb') as fd:
        v = fd.read()
    if size and len(v) != size:
        if verbose: print("wrong size for %s" % f)
        raise ValueError('corrupted blob: %s is not %s bytes' % (f, size))
    return v

# msg format: 0x66|id[32]|alpha[32]
def get(conn, msg):
    _, msg = pop(msg,1)
    id, msg = pop(msg,32)
    alpha, msg = pop(msg,32)
    if msg!=b'':
      if verbose: print('invalid get msg, trailing content %r' % msg)
      fail(conn)

    id = binascii.hexlify(id).decode()
    k = load_blob(id,'key',33)
    if k is None:
      # maybe execute protocol with static but random value to not leak which host ids exist?
      fail(conn)

    rules = load_blob(id,'rules', RULE_SIZE)
    if rules is None:
        fail(conn)

    try:
        beta = pyoprf.evaluate(k[1:], alpha)
    except:
      fail(conn)

    conn.send(k[:1]+beta+rules)

def auth(s,id,alpha):
  pk = load_blob(id,'pub',32)
  if pk is None:
    print('no pubkey found in %s' % id)
    fail(s)
  nonce=pysodium.randombytes(32)
  k = load_blob(id,'key')
  if k is not None:
    try:
       beta = bytes([k[0]])+pyoprf.evaluate(k[1:], alpha)
    except:
       fail(s)
  else:
    beta = b''
  s.send(b''.join([beta,nonce]))
  sig = s.recv(64)
  try:
    pysodium.crypto_sign_verify_detached(sig, nonce, pk)
  except:
    print('bad sig')
    fail(s)
  else:
    s.send(b'\x00\x04auth') # plaintext :/

def change(conn, msg):
  op,   msg = pop(msg,1)
  id,   msg = pop(msg,32)
  alpha,msg = pop(msg,32)
  if msg!=b'':
    if verbose: print('invalid get msg, trailing content %r' % msg)
    fail(conn)

  id = binascii.hexlify(id).decode()
  tdir = os.path.join(datadir,id)
  if not os.path.exists(tdir):
    if verbose: print("%s doesn't exist" % tdir)
    fail(conn)

  auth(conn, id, alpha)

  alpha = conn.recv(32)
  if(len(alpha)!=32):
    fail(conn)

  k=pysodium.randombytes(32)

  try:
      beta = pyoprf.evaluate(k, alpha)
  except:
    fail(conn)

  conn.send(beta)

  blob = conn.recv(32+RULE_SIZE+64)
  if len(blob)!=32+RULE_SIZE+64:
    fail(conn)

  pk = blob[:32]
  try:
    rules = verify_blob(blob,pk)[32:]
  except ValueError:
    fail(conn)

  save_blob(id,'new',k)
  save_blob(id,"rules.new", rules)
  save_blob(id,"pub.new", pk)
  conn.send(b'ok')

def change_dkg(s, msg):
  op,   msg = pop(msg,1)
  id,   msg = pop(msg,32)
  alpha,msg = pop(msg,32)
  if msg!=b'':
    if verbose: print('invalid get msg, trailing content %r' % msg)
    fail(s)

  aux = b'%s%s' % (op, alpha)

  id = binascii.hexlify(id).decode()
  tdir = os.path.join(datadir,id)
  if not os.path.exists(tdir):
    if verbose: print("%s doesn't exist" % tdir)
    fail(s)

  auth(s, id, alpha)

  msg = s.recv(35)
  t,      msg = pop(msg,1,lambda x: x[0])
  n,      msg = pop(msg,1,lambda x: x[0])
  index,alpha = pop(msg,1,lambda x: x[0])

  xi = dkg(s,n,t, index, aux)

  #k=pysodium.randombytes(32)
  try:
      beta = pyoprf.evaluate(xi[1:], alpha)
  except:
    fail(s)

  s.send(bytes([xi[0]])+beta)

  blob = s.recv(32+RULE_SIZE+64)
  if len(blob)!=32+RULE_SIZE+64:
    fail(s)

  pk = blob[:32]
  try:
    rules = verify_blob(blob,pk)[32:]
  except ValueError:
    fail(s)

  save_blob(id,'new',xi)
  save_blob(id,"rules.new", rules)
  save_blob(id,"pub.new", pk)
  s.send(b'ok')

def delete(conn, msg):
  op,   msg = pop(msg,1)
  id,   msg = pop(msg,32)
  alpha,msg = pop(msg,32)
  if msg!=b'':
    if verbose: print('invalid get msg, trailing content %r' % msg)
    fail(conn)

  id = binascii.hexlify(id).decode()
  tdir = os.path.join(datadir,id)
  if not os.path.exists(tdir):
    if verbose: print("%s doesn't exist" % tdir)
    fail(conn)

  auth(conn, id, alpha)

  update_blob(conn)

  shutil.rmtree(tdir)
  conn.send(b'ok')

def commit_undo(conn, msg, new, old):
  op,   msg = pop(msg,1)
  id,   msg = pop(msg,32)
  alpha,msg = pop(msg,32)
  if msg!=b'':
    if verbose: print('invalid get msg, trailing content %r' % msg)
    fail(conn)

  id = binascii.hexlify(id).decode()
  tdir = os.path.join(datadir,id)
  if not os.path.exists(tdir):
    if verbose: print("%s doesn't exist" % tdir)
    fail(conn)

  auth(conn, id, alpha)

  if (new_rules:=load_blob(id,'rules.%s' % new, RULE_SIZE)) is None:
    fail(conn)
  if (cur_rules:=load_blob(id,'rules', RULE_SIZE)) is None:
    fail(conn)
  if (new_pub:=load_blob(id,'pub.%s' % new, 32)) is None:
    fail(conn)
  if (cur_pub:=load_blob(id,'pub', 32) )is None:
    fail(conn)
  if (new_key:= load_blob(id, new, 33)) is None:
    fail(conn)
  if (cur_key:= load_blob(id, 'key', 33)) is None:
    fail(conn)

  save_blob(id,old,cur_key)
  #clearmem(cur_key)
  save_blob(id,"rules.%s" % old, cur_rules)
  save_blob(id,"pub.%s" % old, cur_pub)

  save_blob(id,"key",new_key)
  #clearmem(new_key)
  save_blob(id,"rules", new_rules)
  save_blob(id,"pub", new_pub)

  os.unlink(os.path.join(tdir,new))
  os.unlink(os.path.join(tdir,"pub.%s" % new))
  os.unlink(os.path.join(tdir,"rules.%s" % new))

  conn.send(b'ok')

def read(conn, msg):
  op,   msg = pop(msg,1)
  id,   msg = pop(msg,32)
  alpha,msg = pop(msg,32)
  id = binascii.hexlify(id).decode()
  auth(conn, id, alpha)

  blob = load_blob(id,'blob')
  if blob is None:
    blob = b''
  conn.send(blob)

def handler(conn, data):
   if verbose:
     print('Data received:',data.hex())

   if data[0:1] == GET:
     get(conn, data)
   elif data[0:1] == CHANGE:
     change(conn, data)
   elif data[0:1] == CHANGE_DKG:
     change_dkg(conn, data)
   elif data[0:1] == DELETE:
     delete(conn, data)
   elif data[0:1] == COMMIT:
     commit_undo(conn, data, 'new', 'old')
   elif data[0:1] == UNDO:
     commit_undo(conn, data, 'old', 'new')
   elif data[0:1] == READ:
     read(conn, data)
   elif verbose:
     print("unknown op: 0x%02x" % data[0])

   conn.close()
   os._exit(0)

def create_challenge(conn):
  req = conn.read(65)
  if req[0:1] == READ:
    if len(req)!=33:
      fail(conn)
  elif len(req)!=65:
    fail(conn)
  now = datetime.datetime.now().timestamp()
  id = binascii.hexlify(req[1:33]).decode()
  diff = load_blob(id,'difficulty',9) # ts: u32, level: u8, count:u32
  if not diff: # no diff yet, use easiest hardness
    n = Difficulties[0]['n']
    k = Difficulties[0]['k']
    level = 0
    count = 0
  else:
    level = struct.unpack("B", diff[0:1])[0]
    count = struct.unpack("I", diff[1:5])[0]
    ts = struct.unpack("I", diff[5:])[0]
    if level >= len(Difficulties):
      print("invalid level in rl_ctx:", level)
      level = len(Difficulties) - 1
      count = 0
    elif ((now - rl_decay) > ts and level > 0): # cooldown, decay difficulty
      periods = int((now - ts) // rl_decay)
      if level >= periods:
        level -= periods
      else:
        level = 0
      count = 0
    else: # increase hardness
      if count >= rl_threshold and (level < len(Difficulties) - 1):
        count = 0
        level+=1
      else:
        count+=1
    n = Difficulties[level]['n']
    k = Difficulties[level]['k']

  if (level == len(Difficulties) - 1) and count>rl_threshold*2:
    print(f"{normal}alert{normal}: someones trying (%d) really hard at: %s" %
          (196, 253, count, id))

  rl_ctx = b''.join([
    struct.pack("B", level),   # level
    struct.pack("I", count),   # count
    struct.pack('I', int(now)) # ts
  ])
  if(verbose): print("rl difficulty", {"level": level, "count": count, "ts": int(now)})
  try:
    save_blob(id, 'difficulty', rl_ctx)
  except FileNotFoundError:
    if diff: raise

  challenge = b''.join([bytes([n, k]), struct.pack('Q', int(now))])

  key = load_blob('', "key", 32)
  if not key:
    key=pysodium.randombytes(32)
    save_blob('','key',key)

  state = pysodium.crypto_generichash_init(32, key)
  pysodium.crypto_generichash_update(state,req)
  pysodium.crypto_generichash_update(state,challenge)
  sig = pysodium.crypto_generichash_final(state,32)

  resp = b''.join([challenge, sig])
  conn.send(resp)

def verify_challenge(conn):
  # read challenge
  challenge = conn.read(1+1+8+32) # n,k,ts,sig
  if(len(challenge)!=42):
    fail(conn)
  n, tmp = pop(challenge,1)
  n = n[0]
  k, tmp = pop(tmp,1)
  k = k[0]
  ts, tmp = pop(tmp,8)
  ts = struct.unpack("Q", ts)[0]
  sig, tmp = pop(tmp,32)

  # read request
  req_type = conn.read(1)
  if req_type[0:1] == READ:
    payload = conn.read(32)
    if len(payload)!=32: fail(conn)
  else:
    payload = conn.read(64)
    if len(payload)!=64: fail(conn)
  req = req_type + payload
  # read mac key
  key = load_blob('', "key", 32)
  if not key:
    fail(conn)

  tosign = challenge[:10]

  state = pysodium.crypto_generichash_init(32, key)
  pysodium.crypto_generichash_update(state,req)
  pysodium.crypto_generichash_update(state,tosign)
  mac = pysodium.crypto_generichash_final(state,32)
  # poor mans const time comparison
  if(sum(m^i for (m, i) in zip(mac,sig))):
    fail(conn)

  now = datetime.datetime.now().timestamp()
  if now - (RL_Timeouts[(n,k)]+rl_gracetime) > ts:
    # solution is too old
    fail(conn)

  solsize = equihash.solsize(n,k)
  solution = conn.read(solsize)
  if len(solution)!= solsize:
    fail(conn)

  seed = b''.join([challenge,req])
  if not equihash.verify(n,k, seed, solution):
    fail(conn)

  handler(conn, req)

def ratelimit(conn):
   op = conn.recv(1)
   if op == CREATE:
     data = CREATE+conn.recv(64)
     create(conn, data)
   elif op == CREATE_DKG:
     data = CREATE_DKG+conn.recv(67)
     create_dkg(conn, data)
   elif op == CHALLENGE_CREATE:
     create_challenge(conn)
   elif op == CHALLENGE_VERIFY:
     verify_challenge(conn)

def main():
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(certfile=ssl_cert, keyfile=ssl_key)

    socket.setdefaulttimeout(timeout)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((address, port))
    except socket.error as msg:
        print('Bind failed. Error Code : %s Message: %s' % (str(msg[0]), msg[1]))
        sys.exit()
    #Start listening on socket
    s.listen()
    kids = []
    try:
        # main loop
        while 1:
            #wait to accept a connection - blocking call
            try:
              conn, addr = s.accept()
            except socket.timeout:
              try:
                pid, status = os.waitpid(-1, os.WNOHANG)
                if pid != 0:
                  print("remove pid", pid)
                  kids.remove(pid)
                continue
              except ChildProcessError:
                continue
            except:
              raise

            if verbose:
                print('{} Connection from {}:{}'.format(datetime.datetime.now(), addr[0], addr[1]))
            conn = ctx.wrap_socket(conn, server_side=True)

            while(len(kids)>max_kids):
                pid, status = os.waitpid(0,0)
                kids.remove(pid)

            pid=os.fork()
            if pid==0:
              ssl.RAND_add(os.urandom(16),0.0)
              try:
                ratelimit(conn)
              except:
                print("fail")
                raise
              finally:
                try: conn.shutdown(socket.SHUT_RDWR)
                except OSError: pass
                conn.close()
              sys.exit(0)
            else:
                kids.append(pid)

            try:
              pid, status = os.waitpid(-1,os.WNOHANG)
              if pid!=0:
                 kids.remove(pid)
            except ChildProcessError: pass

    except KeyboardInterrupt:
        pass
    s.close()

if __name__ == '__main__':
  main()
