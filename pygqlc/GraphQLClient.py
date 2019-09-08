import requests
import json
import time
import threading
import websocket
import pydash as py_
from singleton_decorator import singleton
from tenacity import (
  retry, 
  retry_if_result, 
  stop_after_attempt, 
  wait_random
)

from .MutationBatch import MutationBatch

GQL_WS_SUBPROTOCOL = "graphql-ws"

def has_errors(result):
  _, errors = result
  return len(errors) > 0

def data_flatten(data, single_child=False):
  if type(data) == dict:
    keys = list(data.keys())
    if len(keys) == 1:
      key = list(data.keys())[0]
      return data_flatten(data[key], single_child)
    else:
      return data # ! various elements, nothing to flatten
  elif single_child and type(data) == list:
    if len(data) == 1:
      return data_flatten(data[0], single_child)
    elif len(data) == 0:
      return None # * Return none if no child was found
    else:
      return data
  else:
    return data # ! not a dict, nothing to flatten

def safe_pop(data, index=0, default=None):
  if len(data) > 0:
    return data.pop(index)
  else:
    return default

# ! Example With:
'''
client = GraphQLClient()
with client.enterEnvironment('dev') as gql:
    data, errors = gql.query('{lines(limit:2){id}}')
    print(data, errors)
'''

# ! Example setEnvironment:
'''
client = GraphQLClient()
client.addEnvironment('dev', "https://heineken.valiot.app/")
client.addHeader(
    environment='dev', 
    header={'Authorization': dev_token})
data, errors = gql.query('{lines(limit:2){id}}')
print(data, errors)
'''

@singleton
class GraphQLClient:
  def __init__(self):
    # * query/mutation related attributes
    self.environments = {}
    self.environment = None
    # * wss/subscription related attributes:
    self.ws_url = None
    self._conn = None
    self._subscription_running = False
    self.subs = {} # * subscriptions running
    self.sub_counter = 0
    self.sub_router_thread = None
    self.closing = False
    self.unsubscribing = False
  
  # * with <Object> implementation
  def __enter__(self):
    return self
  
  def __exit__(self, type, value, traceback):
    self.environment = self.save_env # restores environment
    return
  
  def enterEnvironment(self, name):
    self.save_env = self.environment
    self.environment = name
    return self # * for use with "with" keyword
  
  # * HIGH LEVEL METHODS ---------------------------------
  # TODO: Implement tenacity in query, mutation and subscription methods
  # @retry(
  #   retry=(retry_if_result(has_errors)),
  #   stop=stop_after_attempt(5),
  #   wait=wait_random(min=0.25, max=0.5))
  # def query_wrapper(self, query, variables=None):
  #   data = None
  #   errors = []
  #   try:
  #     result = self.execute(query, variables)
  #     data = result.get('data', None)
  #     errors = result.get('errors', [])
  #   except Exception as e:
  #     errors = [{'message': str(e)}]
  #   return data, errors
  
  # * Query high level implementation
  def query(self, query, variables=None, flatten=True, single_child=False):
    data = None
    errors = []
    try:
      response = self.execute(query, variables)
      if flatten:
        data = response.get('data', None)
      else:
        data = response
      errors = response.get('errors', [])
      if flatten:
        data = data_flatten(data, single_child=single_child)
    except Exception as e:
      errors = [{'message': str(e)}]
    return data, errors

  # * Query high level implementation
  def query_one(self, query, variables=None):
    return self.query(query, variables, flatten=True, single_child=True)
  
  # * Mutation high level implementation
  def mutate(self, mutation, variables=None, flatten=True):
    response = {}
    data = None
    errors = []
    try:
      response = self.execute(mutation, variables)
    except Exception as e:
      errors = [{'message': str(e)}]
    finally:
      errors.extend(response.get('errors', []))
      if(not errors):
        data = response.get('data', None)
      if flatten:
        data = data_flatten(data)
        if (data):
          errors.extend(data.get('messages', []))
    return data, errors
  # * Subscription high level implementation ******************
  def subscribe(self, query, variables=None, callback=None, flatten=True):
    # ! initialize websocket only once
    if not self._conn:
      env = self.environments.get(self.environment, None)
      self.ws_url = env.get('wss')
      self._conn = websocket.create_connection(self.ws_url,subprotocols=[GQL_WS_SUBPROTOCOL])
    self._conn_init()
    payload = {'query': query, 'variables': variables}
    _cb = callback if callback is not None else self._on_message
    _id = self._start(payload)
    self.subs[_id].update({
      'thread': threading.Thread(target=self._subscription_loop, args=(_cb, _id)),
      'flatten': flatten,
      'queue': [],
      'runs': 0,
    })
    self.subs[_id]['thread'].start()
    # ! Create unsubscribe function for this specific thread:
    def unsubscribe():
      return self._unsubscribe(_id)
    self.subs[_id].update({'unsub': unsubscribe})
    return unsubscribe
  
  def _unsubscribe(self, _id):
    self.unsubscribing = True
    self.subs[_id]['kill'] = True
    self._stop(_id)
    self.subs[_id]['thread'].join()
    self.subs[_id].update({'running': False})
    self.unsubscribing = False
  
  def _sub_routing_loop(self):
    while not self.closing:
      starting = len(self.subs.items()) == 0
      if starting or self.unsubscribing:
        time.sleep(0.01)
        continue
      message = json.loads(self._conn.recv())
      if message['type'] == 'data':
        _id = py_.get(message, 'id')
        self.subs[_id]['queue'].append(message)
      elif message['type'] in['connection_ack', 'ka', 'complete']:
        pass
      else:
        print(f'unknown msg type: {message}')
      time.sleep(0.01)
  
  def _subscription_loop(self, _cb, _id):
    self.subs[_id].update({'running': True})
    while self.subs[_id]['running']:
      aborted = self.subs[_id]['kill']
      if aborted:
        print(f'stopping subscription {_id} on Unsubscribe')
        break
      message = safe_pop(self.subs[_id]['queue'])
      if not message:
        time.sleep(0.01)
        continue
      if message['type'] == 'error' or message['type'] == 'complete':
        print(f'stopping subscription {_id} on {message["type"]}')
        break
      if message['type'] == 'connection_ack' or message['type'] == 'ka':
        pass
      else:
        # * GraphQL message received, proccess it:
        gql_msg = self._clean_sub_message(_id, message)
        _cb(gql_msg)
        self.subs[_id]['runs'] += 1 # take note of how many times this sub has been triggered
      time.sleep(0.01)
  
  def _clean_sub_message(self, _id, message):
    data = py_.get(message, 'payload', {})
    return data_flatten(data) if self.subs[_id]['flatten'] else data

  def close(self):
    # ! ask subscription message router to stop
    self.closing = True
    if not self.sub_router_thread:
      print('connection not stablished, nothing to close')
      return
    for _, sub in self.subs.items():
      sub['unsub']()
    self.sub_router_thread.join()
    self._conn.close()
    self.sub_router_thread = None
    self._conn = None
    self.sub_counter = 0
    self.subs = {}
    self.closing = False
  
  def _on_message(self, message):
    '''Dummy callback for subscription'''
    print(f'message received on subscription:')
    print(message)

  def _conn_init(self):
    env = self.environments.get(self.environment, None)
    headers = env.get('headers', {})
    payload = {
      'type': 'connection_init',
      'payload': headers
    }
    self._conn.send(json.dumps(payload))
    self._conn.recv()
    if not self.sub_router_thread:
      print('first subscription, starting routing loop')
      self.sub_router_thread = threading.Thread(target=self._sub_routing_loop)
      self.sub_router_thread.start()

  def _start(self, payload):
    self.sub_counter += 1
    _id = self.sub_counter # gen_id() # ! Probably requires auto-increment, more than random ID generation
    self.subs.update({_id: {'running': False, 'kill': False}})
    frame = {'id': _id, 'type': 'start', 'payload': payload}
    self._conn.send(json.dumps(frame))
    return _id

  def _stop(self, _id):
    payload = {'id': _id, 'type': 'stop'}
    self._conn.send(json.dumps(payload))
  
  # * END SUBSCRIPTION functions ******************************

  # * BATCH functions *****************************************
  def batchMutate(self, label='mutation'):
    return MutationBatch(client=self, label=label)
  
  def batchQuery(self, label='query'):
    return MutationBatch(client=self, label=label)

  # * END BATCH function **************************************
  # * helper methods
  def addEnvironment(self, name, url=None, wss=None, headers={}, default=False):
    self.environments.update({
      name: {
        'url': url,
        'wss': wss,
        'headers': headers
      }
    })
    if default:
      self.setEnvironment(name)
  def setUrl(self, environment=None, url=None):
    # if environment is not selected, use current environment
    if not environment:
      environment = self.environment
    self.environments[environment].update({'url': url})
  
  def setWss(self, environment=None, url=None):
    # if environment is not selected, use current environment
    if not environment:
      environment = self.environment
    self.environments[environment].update({'wss': url})
  
  def addHeader(self, environment=None, header={}):
    # if environment is not selected, use current environment
    if not environment:
      environment = self.environment
    headers = self.environments[environment].get('headers', {})
    headers.update(header)
    self.environments[environment]['headers'].update(headers)

  def setEnvironment(self, name):
    env = self.environments.get(name, None)
    if not env:
      raise Exception(f'selected environment not set ({name})')
    self.environment = name

  # * LOW LEVEL METHODS ----------------------------------
  def execute(self, query, variables=None):
    data = {
      'query': query,
      'variables': variables
    }
    env = self.environments.get(self.environment, None)
    if not env:
      raise Exception(f'cannot execute query without setting an environment')
    headers = {
      'Accept': 'application/json',
      'Content-Type': 'application/json'
      }
    env_headers = env.get('headers', None)
    if env_headers:
      headers.update(env_headers)
    response = requests.post(env['url'], json=data, headers=headers)
    if response.status_code == 200:
      return response.json()
    else:
      raise Exception(
        "Query failed to run by returning code of {}.\n{}".format(
          response.status_code, query))
