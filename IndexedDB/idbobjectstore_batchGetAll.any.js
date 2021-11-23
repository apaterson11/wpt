// META: title=Batch Get All
// META: script=support.js

'use strict';

const alphabet = 'abcdefghijklmnopqrstuvwxyz'.split('');

function batchgetall_test(func, name) {
  indexeddb_test(
    (t, connection, tx) => {
      let store = connection.createObjectStore('generated',
        { autoIncrement: true, keyPath: 'id' });
      alphabet.forEach(letter => {
        store.put({ ch: letter });
      });

      store = connection.createObjectStore('out-of-line', null);
      alphabet.forEach(letter => {
        store.put(`value-${letter}`, letter);
      });

      store = connection.createObjectStore('empty', null);
    },
    func,
    name
  );
}

function createBatchGetAllRequest(t, storeName, connection, ranges, maxCount) {
  const transaction = connection.transaction(storeName, 'readonly');
  const store = transaction.objectStore(storeName);
  const req = store.batchGetAll(ranges, maxCount);
  req.onerror = t.unreached_func('getAll request should succeed');
  return req;
}

batchgetall_test((t, connection) => {
  const req = createBatchGetAllRequest(t, 'out-of-line', connection, ['c']);
  req.onsuccess = t.step_func(evt => {
    assert_equals(JSON.stringify(evt.target.result), JSON.stringify([['value-c']]));
    t.done();
  });
}, 'Single item get');

batchgetall_test((t, connection) => {
  const req = createBatchGetAllRequest(t, 'empty', connection);
  req.onsuccess = t.step_func(evt => {
    assert_array_equals(evt.target.result, [],
      'getAll() on empty object store should return an empty array');
    t.done();
  });
}, 'batchGetAll on empty object store');


batchgetall_test((t, connection) => {
  const req = createBatchGetAllRequest(t, 'out-of-line', connection, ['c', 'dd', 'e', 'ff']);
  req.onsuccess = t.step_func(evt => {
    assert_equals(JSON.stringify(evt.target.result), JSON.stringify([['value-c'], [], ['value-e'], []]));
    t.done();
  });
}, 'batchGetAll with non-existing values');


batchgetall_test((t, connection) => {
  const req = createBatchGetAllRequest(t, 'out-of-line', connection, [IDBKeyRange.bound('a', 'z')],
    5);
  req.onsuccess = t.step_func(evt => {
    assert_equals(JSON.stringify(evt.target.result), JSON.stringify([['value-a', 'value-b', 'value-c', 'value-d', 'value-e']]));
    t.done();
  });
}, 'Get bound range with maxCount');


batchgetall_test((t, connection) => {
  const req = createBatchGetAllRequest(t, 'out-of-line', connection,
    [IDBKeyRange.bound('a', 'e')]);
  req.onsuccess = t.step_func(evt => {
    assert_equals(JSON.stringify(evt.target.result), JSON.stringify([['value-a', 'value-b', 'value-c', 'value-d', 'value-e']]));
    t.done();
  });
}, 'Get bound range');

batchgetall_test((t, connection) => {
  const req = createBatchGetAllRequest(t, 'out-of-line', connection,
    [IDBKeyRange.bound('g', 'k', false, true), IDBKeyRange.bound('g', 'k', true, false)]);
  req.onsuccess = t.step_func(evt => {
    assert_equals(JSON.stringify(evt.target.result), JSON.stringify([['value-g', 'value-h', 'value-i', 'value-j'], ['value-h', 'value-i', 'value-j', 'value-k']]));
    t.done();
  });
}, 'Get upper/lower excluded');

batchgetall_test((t, connection) => {
  const req = createBatchGetAllRequest(t, 'generated', connection,
    [IDBKeyRange.bound(4, 15), IDBKeyRange.bound(5, 15)], 3);
  req.onsuccess = t.step_func(evt => {
    const data = evt.target.result;
    assert_array_equals(data[0].map(e => { return e.ch; }), ['d', 'e', 'f']);
    assert_array_equals(data[0].map(e => { return e.id; }), [4, 5, 6]);
    assert_array_equals(data[1].map(e => { return e.ch; }), ['e', 'f', 'g']);
    assert_array_equals(data[1].map(e => { return e.id; }), [5, 6, 7]);
    t.done();
  });
}, 'Get bound range (generated) with maxCount');


batchgetall_test((t, connection) => {
  const req = createBatchGetAllRequest(t, 'out-of-line', connection, [IDBKeyRange.bound('a', 'z')], 0);
  req.onsuccess = t.step_func(evt => {
    assert_array_equals(evt.target.result[0],
      alphabet.map(c => { return 'value-' + c; }));
    t.done();
  });
}, 'zero maxCount');
