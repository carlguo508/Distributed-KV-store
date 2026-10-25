# Design a distributed key-value store


## The problem

Imagine that several internal services need to store and retrieve small pieces of
application state by a unique key. Examples include a job's current status or a service's
configuration. These services need a shared, durable store that can grow across multiple
machines.

Your task is to design and implement a distributed key-value store.  

## Functional requirements

Support three operations:

- **`Put(key, value)`:** Create a key or replace its entire value. Report success after
  the write satisfies the durability requirement below.
- **`Get(key)`:** Return the stored value, or an explicit `NotFound` result if the key
  does not exist.
- **`Delete(key)`:** Remove the key. Deleting an absent key succeeds.

Keys and values are opaque byte sequences. An empty value is valid and is different from a
missing key. Multiple clients may operate on the same key at the same time. Each operation
affects exactly one key.

You may define the request and response format. 

## Correctness and reliability requirements
There is a set of minimum requirements that need to be supported:

- **Per-key consistency:** If a `Put` completes successfully and another client
  subsequently calls `Get` on that key, the read must return that value or a value from a
  later write. After a successful `Delete`, a subsequent read must return `NotFound`
  unless a later write recreates the key. Concurrent operations on a key must behave as if
  they occurred in some single order. There is no ordering requirement between different
  keys.
- **Restarts:** Servers must recover their data after a process restart; memory alone is
  not sufficient.

The following is a set of optional (nice-to-have) requirements: 
- **Durability:** Successful writes and deletes must survive the permanent loss of any one
  storage server, including its disk, within the supported failure model.
- **Availability after a server failure:** A single storage server failure may cause a
  brief interruption while the system detects the failure and recovers. Unaffected data
  should remain accessible, and the affected data should become accessible through
  surviving servers.
- **Network partitions:** Do not return stale data as a successful read. If a group of
  servers cannot safely satisfy the consistency requirement, it may return an unavailable
  error or time out.

Assume a single region deployment of your system, i.e. you don't need to consider 
entire region outages. Assume a static deployment where you pre-define the number of 
storage servers. The number of storage servers can range from 1-4. No need to worry
about authentication. 

## Workload characteristics and performance requirements

### Workload characteristics
- **Key size:** 32 bytes on average; maximum 256 bytes
- **Value size:** 1 KiB on average; maximum 64 KiB
- **Operation mix:** Approximately 80% reads and 20% writes, including deletes
- **Traffic:** 1,000 operations/second sustained
- **Clients:** Internal services in the same region

### Performance requirements
-- **Latency goals:** Healthy-system p99 at sustained load below 50 ms for reads and 100 ms
  for writes, measured at the service boundary

## Deliverables
- Source code of your solution
- Scripts to start and stop the system. You should be able to deploy your system on a single 
  machine where each storage server is a separate process. 
- A workload generator that issues requests against your system. You need to be able to
  demonstrate that the system is working and that is can sustain the latency goals at the 1K ops/second throughput.
- Coding agent prompts used to build the system
