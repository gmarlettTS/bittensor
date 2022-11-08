##################
##### Import #####
##################
import torch
import concurrent.futures
import time
import psutil
import sys
import random
import argparse
from torch.utils.data.dataloader import DataLoader
from tqdm import tqdm
import bittensor
import glob
from queue import Queue
import numpy as np
import asyncio
import aiohttp
import json
import os
import threading
import asyncio
import nest_asyncio
##########################
##### Get args ###########
##########################
from typing import *

import streamlit as st

class ThreadManager:
    """ Base threadpool executor with a priority queue 
    """

    def __init__(self,  max_threads=None):
        """Initializes a new ThreadPoolExecutor instance.
        Args:
            max_threads: The maximum number of threads that can be used to
                execute the given calls.
            thread_name_prefix: An optional name prefix to give our threads.
            initializer: An callable used to initialize worker threads.
            initargs: A tuple of arguments to pass to the initializer.
        """
        self.max_threads = max_threads
        self._idle_semaphore = threading.Semaphore(0)
        self._threads = []
        self._shutdown_lock = threading.Lock()
        self._shutdown = False

    def submit(self, fn, args=[],kwargs={}):
        with self._shutdown_lock:
            if self._shutdown:
                raise RuntimeError('cannot schedule new futures after shutdown')
            
            thread = threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True)
            thread.start()
            self._threads.append(thread)

        return thread


    @property
    def threads(self):
        return self._threads

    def __del__(self):
        self.shutdown()

    def shutdown(self, wait=True):
        if wait:
            for t in self._threads:
                try:
                    t.join()
                except Exception:
                    pass




class Dataset:
    """ Implementation for the dataset class, which handles dataloading from ipfs
    """

    ipfs_url = 'http://global.ipfs.opentensor.ai/api/v0'
    dataset_dir = 'http://global.ipfs.opentensor.ai/api/v0/cat' 
    text_dir = 'http://global.ipfs.opentensor.ai/api/v0/object/get'
    mountain_hash = 'QmSdDg6V9dgpdAFtActs75Qfc36qJtm9y8a7yrQ1rHm7ZX'

    def __init__(self, 
                batch_size: int=8, 
                sequence_length: int=256,
                block_size: int = 10000,
                num_workers: int = 1,
                dataset_name: List[str]=['ArXiv'], 
                loop:'asyncio.loop'=None, 
                tokenizer:'bittensor.tokenizer'=None, 
                no_tokenizer: bool = False,
                data_dir: str =  os.path.expanduser('~/./bittensor/dataset'),
                save_dataset : bool = False,
                load_dataset : bool = True,
                run_generator:bool=True,
                buffer_size:int=100,
                num_batches: int = 100,
                max_datasets: int = 2,
                max_directories: int=10):


        """
        Args:
            loop (asyncio.loop):
                The asyncio loop, defaults to default event loop
            
            tokenizer (bittensor.tokenizer):
                Tokenizer, defaults to bittensor.tokenizer
            
            dataset_name (List[str]):
                The list of dataset names to laod
            
            run_generator (bool): 
                Run the generator
            
            buffer_size (int):
                The size of the buffer for the generator.

        """
        self.__infinite_dataset_iterator = None
        self.dataset_size = 0
        self.batch_size = batch_size
        self.block_size = block_size
        self.num_workers = num_workers
        self.sequence_length = sequence_length
        self.dataset_name = dataset_name
        self.set_event_loop(loop=loop)
        # if datasets is None then refer to all of the availabe datasets 
        self.max_datasets = max_datasets
        if len(self.dataset_name) == 0 or self.dataset_name == 'default':
            self.dataset_name = self.available_datasets
        self.dataset_name = self.dataset_name[:self.max_datasets]
        self.no_tokenizer = no_tokenizer
        if not  self.no_tokenizer:
            self.set_tokenizer(tokenizer=tokenizer)
        
        self.data_dir =  data_dir
        self.save_dataset = save_dataset
        self.load_dataset = load_dataset
        self.run_generator= run_generator
        self.buffer_size = buffer_size
        self.num_batches = num_batches
        self.max_directories = max_directories

        # we need to build the dataset or load existing text file hashes
        # notice the heirarchy of ipfs hashes is DATASET -> FOLDER -> TEXT HASH, 
        # we want to flatten each dataset FOLDER -> TEXT HASH into FOLDER*TEXT
        
        import streamlit as st

        self.build_datasets(datasets=self.dataset_name, load=self.load_dataset, save=self.save_dataset)

        self.data_queue = Queue(buffer_size)
        # this runs the a thread that has its own asyncio loop. 
        # The loop is passed into nested async functions to use loo.run_until_complete function
        if self.run_generator:
            # the thread manager is used for running a background thread
            self.thread_manager = ThreadManager()
            # start the genrator
            self.thread_manager.submit(fn=self.sample_generator, kwargs=dict(queue=self.data_queue, loop=asyncio.new_event_loop()))
    def sample_generator(self, 
                         queue:Queue, 
                         loop:'asyncio.loop'=None, 
                         return_json:bool=False):

        """ Sample generator on seperate thread with its own asyncio loop for generating
            background samples while the user fetches them in the foreground.
        Args:
            queue (Queue):
                Queue for feeding the samples through for __getitem__ to fetch.
            batch_size (int):
                Batch size of the samples.
            sequence_length (int):
                Sequence Length of the samples.
            loop:'asyncio.loop'=None, 
                        return_json:bool=False

        Returns: None
        """
        
        # this is for starting a new thread
        # the loop needs to be set within the new thread
        if loop != None:
            asyncio.set_event_loop(loop)

        # chunk the text hashes into batch_sie chunks
        text_hash_batch_list = self.chunk(self.all_text_file_metas,
                                chunk_size=batch_size,
                                append_remainder=False,
                                distribute_remainder=False,
                                num_chunks= None)

        # run through each chunk, then tokenize it,

        batch_count = 0
        self
        

        for text_hash in self.all_text_file_metas:

            if batch_count > self.num_batches:
                break

            raw_text = self.async_run(self.get_text(text_hash=text_hash), loop=loop)


            if not queue.full():
            # skip queue if it is full

                queue.put(raw_text)

    def build_datasets(self, datasets:List[str], save:bool=False, load:bool=True, loop:'asyncio.loop'=None):
        """ Building all of the datasets specified by getting each of their 
            text hashes from IPFS or local
        Args:
            datasets (List[str]):
                Axon to serve.s
            save (bool):
                Save the dataset hashes locally.
            load (bool):
                Load the dataset hashes locally
            loop (asyncio.Loop):
                Asyncio loop 

        Returns: None
        """
        self.dataset_size = 0

        all_text_file_metas = []
        dataset_hash_map = {}

        if len(dataset_hash_map) == 0:
            tasks = []

            # gather dataset hashes async as their state is independent
            for dataset in datasets:
                tasks += [self.build_dataset(dataset=dataset, save=save, load=load, loop=loop)]

            dataset_hashes = self.async_run(asyncio.gather(*tasks), loop=loop)

            # create a hash map of dataset -> text hashes
            for k,v in zip(datasets, dataset_hashes):
                if len(v) > 0:
                    dataset_hash_map[k] = v
                    

        self.dataset_size_map = {}
        self.dataset_hash_map = dataset_hash_map
        for  k,file_meta_list in dataset_hash_map.items():
            all_text_file_metas += v
            self.dataset_size_map[k] =  sum([f['Size'] for f in file_meta_list])
            self.dataset_size += self.dataset_size_map[k]
        self.all_text_file_metas = all_text_file_metas

    async def async_save_json(self, 
                              path:str,
                              obj:Union[dict, list],
                              include_root:bool=True) -> str:
        """ 
        Async save of json for storing text hashes

        Args:
            path (List[str]):
                Axon to serve.
            obj (bool):
                The object to save locally
            include_root (bool):
                Include self.data_dir as the prefix.
                    - if True, ths meants shortens the batch and 
                    specializes it to be with respect to the dataset's 
                    root path which is in ./bittensor/dataset
            
        Returns: path (str)
            Path of the saved JSON
        """
        
        if include_root:
            path = os.path.join(self.data_dir, path)

        dir_path = os.path.dirname(path)

        # ensure the json is the prefix
        if path[-len('.json'):] != '.json':
            path += '.json'

        # ensure the directory exists, make otherwise
        if not os.path.isdir(dir_path):
            os.makedirs(dir_path)

        with open(path, 'w') as outfile:
            json.dump(obj, outfile)

        return path

    def save_json(self,loop:'asyncio.loop'=None, *args,**kwargs) -> str:
        '''
        Sync verson of async_save_json

        Args
            loop (asyncio.loop):
                The asyncio loop to be past, otherwise self.loop

        Returns (str) 

        '''
        return self.async_run(self.async_save_json(*args,**kwargs),loop=loop)

    async def async_load_json(self, path:str,include_root:bool=True, default:Union[list, dict]={}) -> Union[list, dict]:

        """ 
        Async save of json for storing text hashes

        Args:
            path (str):
                Path of the loaded json

            include_root (bool):
                Include self.data_dir as the prefix.
                    - if True, ths meants shortens the batch and 
                    specializes it to be with respect to the dataset's 
                    root path which is in ./bittensor/dataset
            
        Returns: path (str)
            Path of the saved JSON
        """
        
        if include_root:
            path = os.path.join(self.data_dir, path)

        # ensure extension
        dir_path = os.path.dirname(path)
        if path[-len('.json'):] != '.json':
            path += '.json'

        # ensure dictionary
        if not os.path.isdir(dir_path):
            os.makedirs(dir_path)

        # load default if file does not exist
        try:
            with open(path, 'r') as f:
                obj = json.load(f)
        except FileNotFoundError:
            obj = default

        if isinstance(obj, str):
            obj = json.loads(obj)

        return obj

    def load_json(self, loop:'asyncio.loop'=None, *args,**kwargs) -> Union[list, dict]:
        '''
        Sync verson of async_save_json

        Args
            loop (asyncio.loop):
                The asyncio loop to be past, otherwise self.loop

        Returns (dict, list) 

        '''
        return self.async_run(job=self.async_load_json(*args,**kwargs), loop=loop)

    async def build_dataset(self, dataset=None, num_folders=10, num_samples=40, save=False, load=True, loop=None):

        folder_hashes = (await self.get_folder_hashes(self.dataset2hash[dataset]))[:num_folders]
        if len(folder_hashes) == 0:
            folder_hashes = [self.dataset2hash[dataset]]
        
        random.shuffle(folder_hashes)

        hash2file_meta = {}
        if load:
            loaded_file_metas =  self.load_json(path=f'{dataset}/file_metas', default=[], loop=loop)
            for file_metas in loaded_file_metas:
                hash2file_meta[file_meta['Hash']] = file_metas

        if len(hash2file_meta)<num_samples:
            for f in folder_hashes:
                self.total = 0
                loaded_file_metas = await self.get_text_file_metas(f)
                for file_meta in loaded_file_metas:

                    hash2file_meta[file_meta['Hash']] = file_meta   
                    if len(hash2file_meta) >=num_samples:
                        break

                if len(hash2file_meta) >=num_samples:
                    break

        text_file_metas = list(hash2file_meta.values())

        if save:
            self.save_json(path=f'{dataset}/file_metas', obj=text_file_metas, loop=loop)
        return text_file_metas


    def idx2filemeta(self, idx:int=0): 
        current_idx = 0 
        for file_meta in self.all_text_file_metas:
            step = file_meta['Size'] // self.block_size
            current_idx += step
            if current_idx >= idx:
                file_meta['start_bytes'] = idx - current_idx
                return file_meta

        raise Exception('This is forbidden terirtory')


    cached_raw_text_list = []
    cache_size = 20
    calls_for_current_block = 0
    calls_per_block = 1000
    def __getitem__(self, idx: int= None) -> Union[str, torch.tensor]:
        '''
        Get the item of the queue (only use when sample_generator is running)
        '''

        if idx == None:
            idx = random.randint(0, self.__len__())
        
        self.calls_for_current_block += 1
        if self.calls_for_current_block>self.calls_per_block:
            if len(self.cached_raw_text_list) >= 1:
                self.cached_raw_text_list = self.cached_raw_text_list[1:]
            self.calls_for_current_block = 0
        if len(self.cached_raw_text_list) < self.cache_size:
            if self.data_queue.empty():
                file_meta = self.idx2filemeta(idx=idx)
                raw_text =  self.async_run(self.get_text(file_meta=file_meta))
            else:
                raw_text = self.data_queue.get()
            self.cached_raw_text_list.append(raw_text)


        raw_text = random.choice(self.cached_raw_text_list)
        if  self.no_tokenizer:
            output_dict = raw_text
        else:
            tokenized_dict = self.tokenizer(raw_text, padding=True)
            output_dict = {}
            for k,v in tokenized_dict.items():
                v = torch.tensor(v[:self.sequence_length])
                seqeunce_length_remainder =  self.sequence_length - v.shape[0]
                if seqeunce_length_remainder:
                    v = torch.nn.functional.pad(input=v, pad=(0,seqeunce_length_remainder), mode='constant', value=0 ) 
                output_dict[k] = v



        return output_dict
    
    async def get_dataset_hashes(self):
        mountain_meta = {'Name': 'mountain', 'Folder': 'meta_data', 'Hash': self.mountain_hash}
        response = await self.api_post( url=f'{self.ipfs_url}/object/get',  params={'arg': mountain_meta['Hash']}, return_json= True)
        response = response.get('Links', None)
        return response

    async def get_folder_hashes(self, 
                                file_meta:dict,
                                num_folders:int = 5) -> List[str]:
        '''
        Get the folder hashes from the dataset.

        Args:
            file_meta (dict):
                File meta contianing the hash and name of the link.
            num_folders (int):
                The number of folders to load at once
        Returns folder_hashes (List[str])
        
        '''
        links = (await self.get_links(file_meta))[:100]

        unfinished = [self.loop.create_task(self.api_post(self.ipfs_url+'/object/get', params={'arg':link['Hash']}, return_json=True)) for link in links]
        folder_hashes = []
        while len(unfinished)>0:
            finished, unfinished = await asyncio.wait(unfinished, return_when=asyncio.FIRST_COMPLETED)
            for res in await asyncio.gather(*finished):
                folder_hashes.extend(res.get('Links'))
        return folder_hashes

    async def get_text_file_metas(self, file_meta:dict, num_hashes:int=50) -> List[str]:
        """
        Get text hashes from a folder

        Args:
            file_meta (dict):
                File meta contianing the hash and name of the link.
            num_hashes:
                The maximum number of hashes before stopping.
        
        Returns List[str]

        """

        try:
            data = await self.api_post(f'{self.ipfs_url}/cat', params={'arg':file_meta['Hash']}, return_json=False, num_chunks=10)
        except KeyError:
            return []
        decoded_hashes = []
        hashes = ['['+h + '}]'for h in data.decode().split('},')]
        for i in range(len(hashes)-1):
            try:
                decoded_hashes += [json.loads(hashes[i+1][1:-1])]
            except json.JSONDecodeError:
                pass

            if len(decoded_hashes) >= num_hashes:
                return decoded_hashes
            # hashes[i] =bytes('{'+ hashes[i+1] + '}')


    total = 0 
    async def get_text(self, file_meta, loop=None, num_blocks=1, queue=None):
        
        """
        Get text hashes from a folder

        Args:
            file_meta (dict):
                File meta contianing the hash and name of the link.
            num_hashes:
                The maximum number of hashes before stopping.
        
        Returns List[str]

        """

        
        if loop == None:
            loop = self.loop
        
        if isinstance(file_meta, str): 
            file_meta  = {'Hash': file_meta}

        assert isinstance(file_meta, dict )
        
        def task_cb(context):
            self.total += len(context.result())

        headers = {}
        # we need to  set the 
        content_type = None
        url = f'{self.ipfs_url}/cat'
        params={'arg':file_meta['Hash']}
        timeout = aiohttp.ClientTimeout(sock_connect=10, sock_read=10)
        async with aiohttp.ClientSession( timeout=timeout) as session:
            async with session.post(url,params=params,headers=headers) as res:

                # return_result = await res.json(content_type=content_type)
                count = 0
                async for data in res.content.iter_chunked(self.block_size):
                    count += 1
                    if isinstance(queue, Queue): 
                        queue.put(str(data))
                    else:
                        if count >= num_blocks:
                            return str(data)


    async def get_links(self, file_meta:dict, **kwargs) -> List[dict]:
        '''
        Get Links from file_meta

        Args
            file_meta (dict): 
                Dictionary containing hash and name of root link
        
        Returns (List[dict])

        '''
        response = await self.api_post( url=f'{self.ipfs_url}/object/get',  params={'arg': file_meta['Hash']}, return_json= True)
        response_links = response.get('Links', [])
        return response_links


    async def api_post(self, 
                      url:str, 
                      return_json:bool = False, 
                      content_type:str=None, 
                      chunk_size:int=1024, 
                      num_chunks:int=None, 
                      **kwargs) -> 'aiohttp.Response':
        
        '''
        async api post

        Args:
            url (str):
                url of endpoint.
            return_json (bool): 
                Return repsonse as json.
            content_type (str):
                Content type of request.
            chunk_size (int):
                Chunk size of streaming endpoint.
            num_chunks (int):
                Number of chunks to stream.
        Returns (aiohttp.Response)
        '''
        headers = kwargs.pop('headers', {}) 
        params = kwargs.pop('params', kwargs)
        return_result = None


        # we need to  set the 
        timeout = aiohttp.ClientTimeout(sock_connect=10, sock_read=10)
        async with aiohttp.ClientSession( timeout=timeout) as session:
            async with session.post(url,params=params,headers=headers) as res:
                if return_json: 
                    return_result = await res.json(content_type=content_type)
                else:
                    return_result = res

                # if num_chunks != None
                if num_chunks:
                    return_result = b''
                    async for data in res.content.iter_chunked(chunk_size):
                        return_result += data
                        num_chunks-= 1
                        if num_chunks == 0:
                            break
        return return_result


    async def api_get(self, 
                      url:str,
                    return_json:bool = True,
                     content_type:str=None, 
                     chunk_size:int=1024, 
                     num_chunks:int=1,
                     **kwargs) -> 'aiohttp.Response':
        '''
        async api post

        Args:
            url (str):
                url of endpoint.
            return_json (bool): 
                Return repsonse as json.
            content_type (str):
                Content type of request.
            chunk_size (int):
                Chunk size of streaming endpoint.
            num_chunks (int):
                Number of chunks to stream.
        Returns (aiohttp.Response)
        '''
        headers = kwargs.pop('headers', {}) 
        params = kwargs.pop('params', kwargs)
        return_result = None
        async with aiohttp.ClientSession(loop=self.loop) as session:
            async with session.get(url,params=params,headers=headers) as res:
                if return_json: 
                    return_result = await res.json(content_type=content_type)
                else:
                    return_result = res

                if chunk_size:
                    return_result = b''
                    async for data in res.content.iter_chunked(chunk_size):
                        return_result += data
                        num_chunks-= 1
                        if num_chunks == 0:
                            break
        return return_result


    ##############
    #   ASYNCIO
    ##############
    @staticmethod
    def reset_event_loop(set_loop:bool=True) -> 'asyncio.loop':
        '''
        Reset the event loop

        Args:
            set_loop (bool):
                Set event loop if true.

        Returns (asyncio.loop)
        '''
        loop = asyncio.new_event_loop()
        if set_loop:
            asyncio.set_event_loop(loop)
        return loop

    def set_event_loop(self, loop:'asyncio.loop'=None)-> 'asynco.loop':
        '''
        Set the event loop.

        Args:
            loop (asyncio.loop):
                Event loop.

        Returns (asyncio.loop)
        '''
        
        if loop == None:
            loop = asyncio.get_event_loop()
        self.loop = loop
        return self.loop
         
    def async_run(self, job, loop=None): 
        '''
        Set the event loop.

        Args:
            job (asyncio.Task)
            loop (asyncio.loop):
                Event loop.

        '''
        
        if loop == None:
            loop = self.loop
        return loop.run_until_complete(job)


    @property
    def dataset2size(self) -> Dict:
        '''
        dataset to the number of hashes in the dataset
        '''
        return {k:v['Size'] for k,v in self.dataset2hash.items()}
    @property
    def available_datasets(self) -> List[str]:
        '''
        list of available datasets
        '''

        return list(self.dataset2hash.keys())
    @property
    def dataset2hash(self) -> Dict:
        '''
        Dictionary to hash
        '''
        return {v['Name'].replace('.txt', '') :v for v in self.dataset_hashes}
    

    @property
    def dataset_hashes(self) -> List[str]:
        '''
        Return the dataset hashes
        '''


        if not hasattr(self, '_dataset_hashes'):
            self._dataset_hashes = self.async_run(self.get_dataset_hashes())
        return self._dataset_hashes
    def set_tokenizer(self, tokenizer:bittensor.tokenizer=None) -> bittensor.tokenizer:
        '''
        Resolve the tokenizer
        '''
        if tokenizer == None:
            tokenizer = bittensor.tokenizer()
        
        self.tokenizer = tokenizer

    @staticmethod
    def chunk(sequence:list,
            chunk_size:str=None,
            append_remainder:bool=False,
            distribute_remainder:bool=True,
            num_chunks:int= None):

        '''
        Chunk a list into N chunks for batching

        Args:
            sequence (list):
                Size of the sequence Length
            chunk_size (str):
                Size of the chunk.
            append_remainder (bool):
                Append the remainder
            distribute_remainder (bool):
                Distribute the remainder as round robin
            num_chunks (int):
                The number of chunks.
        Returns (int)
        '''

        # Chunks of 1000 documents at a time.

        if chunk_size is None:
            assert (type(num_chunks) == int)
            chunk_size = len(sequence) // num_chunks

        if chunk_size >= len(sequence):
            return [sequence]
        remainder_chunk_len = len(sequence) % chunk_size
        remainder_chunk = sequence[:remainder_chunk_len]
        sequence = sequence[remainder_chunk_len:]
        sequence_chunks = [sequence[j:j + chunk_size] for j in range(0, len(sequence), chunk_size)]

        if append_remainder:
            # append the remainder to the sequence
            sequence_chunks.append(remainder_chunk)
        else:
            if distribute_remainder:
                # distributes teh remainder round robin to each of the chunks
                for i, remainder_val in enumerate(remainder_chunk):
                    chunk_idx = i % len(sequence_chunks)
                    sequence_chunks[chunk_idx].append(remainder_val)

        return sequence_chunks


    def dataloader(self, epoch_length = 100):
        """ Creates a torch dataloader out of a subclass of this class.

        Args:
            epoch_length (int, optional): The epoch length of the miner. If this length is not set or if it is larger than the dataset,
            then a dataloader for the entire dataset is returned. Otherwise, a dataloader for a subset of the dataset of epoch_length
            is returned. Defaults to None.

        Returns:
            torch.utils.data.dataloader.DataLoader: Pytorch dataloader.
        """

        return DataLoader(self,
                    shuffle=True,
                    batch_size=self.batch_size,
                    num_workers=self.num_workers,
                    drop_last=True)
    

    def __next__(self):
        """Returns the next element from the dataset. 
        """
        if self.__infinite_dataset_iterator == None:
            self.__infinite_dataset_iterator = iter(self.dataloader())

        try:
            return next(self.__infinite_dataset_iterator)
        except StopIteration:
            self.__infinite_dataset_iterator = iter(list(self.dataloader()))
            return next(self.__infinite_dataset_iterator)


    def __del__(self):
        del self.thread_manager
        

    def __len__(self):
        """Returns number of samples (blocks) of dataset

        Returns:
            length: int
        """

        return self.dataset_size // self.block_size
