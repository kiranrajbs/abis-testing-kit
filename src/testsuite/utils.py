import errno
import json
import os
import time
import uuid
from typing import List, Dict

import base64
from cbeff import Biometrics
from .models import RequestMap, Tests, Logs
from orchestrator import parse_test_cases, insert, identify, delete, ping, reference_count, save_file, criteria_resolver


def parse_biometric_file(name: str, path: str):
    strs = name.split('.')[0].split('_')
    if not name.endswith('.jpeg'):
        return False, None, "only jpeg format is allowed"
    if len(strs) != 2:
        return False, None, "filename should be <Biometric type>_<Biometric subtype>"

    with open(path, 'rb') as file:
        data = file.read()
        data = base64.b64encode(data).decode('utf-8')
        print("-----------------------------------------------------------------------------------------")
        print(type(data))
    biometric = Biometrics(strs[0], strs[1], data)
    return True, biometric, None


class Orchestrator:

    def __init__(self, run_id, run_type):
        self.run_id = run_id
        self.run_type = run_type
        self.log_tx = []
        self.store = {}
        return

    def run(self):
        Tests.objects.filter(run_id=self.run_id).update(status="in-progress")
        Logs(run_id=self.run_id, log="Run: "+self.run_id+" is in progress").save()
        request_ids = []
        responses = []
        print("Run id: " + self.run_id)
        log_abs_path = os.path.abspath(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), './../', 'result', self.run_id + '.json'))
        store_abs_path = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), './../', 'store'))
        test_case_file_path = os.path.join(store_abs_path, 'test_cases.json')
        test_data_file_path = os.path.join(store_abs_path, 'test_data.json')
        if not os.path.isfile(test_case_file_path):
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), test_case_file_path)
        if not os.path.isfile(test_data_file_path):
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), test_data_file_path)

        with open(test_case_file_path, 'r') as file:
            test_cases: List = json.loads(file.read())

        with open(test_data_file_path, 'r') as file:
            test_data: List = json.loads(file.read())

        self.store = {}
        for key, val in enumerate(test_data):
            self.store[val['name']] = val

        parsed_test_cases = parse_test_cases(test_cases)

        for ptc in parsed_test_cases:
            Logs(run_id=self.run_id, log="Test: "+ptc['testId']+" is running.").save()
            ptc['runId'] = self.run_id
            for idx, val in enumerate(ptc['steps']):
                st = ptc['steps'][idx]
                status, msg, request, request_id = (None,) * 4
                request_id = uuid.uuid4().hex
                if st['method'] == 'insert':
                    status, msg, request = self.run_insert(st, request_id)
                    ptc['steps'][idx]['request_id'] = request_id
                elif st['method'] == 'identify':
                    status, msg, request = self.run_identify(st, request_id)
                    ptc['steps'][idx]['request_id'] = request_id
                elif st['method'] == 'delete':
                    status, msg, request = self.run_delete(st, request_id)
                    ptc['steps'][idx]['request_id'] = request_id
                elif st['method'] == 'ping':
                    status, msg, request = self.run_ping(request_id)
                    ptc['steps'][idx]['request_id'] = request_id
                elif st['method'] == 'reference_count':
                    status, msg, request = self.run_reference_count(request_id)
                    ptc['steps'][idx]['request_id'] = request_id

                ptc['steps'][idx]['status'] = status
                ptc['steps'][idx]['msg'] = msg
                ptc['steps'][idx]['request'] = request
                request_ids.append(request_id)

                if self.run_type == "sync":
                    Logs(run_id=self.run_id, log="Test: " + ptc['testId'] + ", checking queue response for request id: "+request_id).save()
                    self.responseChecker([request_id])

            ptc['store'] = self.store

            if self.run_type != "sync":
                Logs(run_id=self.run_id,
                     log="Test: " + ptc['testId'] + ", checking queue response for request ids: " + str(request_ids)).save()
                self.responseChecker(request_ids)

            for idx, val in enumerate(ptc['steps']):
                request_map = RequestMap.objects.filter(request_id=val['request_id']).first()
                if request_map is not None:
                    response = request_map.response.replace("\'", "\"")
                    ptc['steps'][idx]['response'] = json.loads(response)
                else:
                    print("possibility of error")
            self.log_tx.append(ptc)
            Logs(run_id=self.run_id, log="Test: "+ptc['testId']+", all steps have been executed").save()

        Logs(run_id=self.run_id, log="Criteria resolver stage: ").save()
        final_results = criteria_resolver(self.log_tx)
        save_file(log_abs_path, final_results)

        for ent in final_results:
            Logs(run_id=self.run_id, log="Test: "+ent['testId']+", result: "+str(ent['results'])).save()
        return

    def run_insert(self, st, request_id):
        person = st['parameters'][0]
        reference_id = self.store[person]['reference_id']
        status, msg, request = insert(request_id, reference_id)
        return status, msg, request

    def run_identify(self, st, request_id):
        person = st['parameters'][0]
        reference_id = self.store[person]['reference_id']
        ref_ids = st['parameters'][1:]
        status, msg, request = identify(request_id, reference_id, ref_ids)
        return status, msg, request

    def run_delete(self, st, request_id):
        person: List = st['parameters'][0]
        reference_id = self.store[person]['reference_id']
        status, msg, request = delete(request_id, reference_id)
        return status, msg, request

    @staticmethod
    def run_ping(request_id):
        status, msg, request = ping(request_id)
        return status, msg, request

    @staticmethod
    def run_reference_count(request_id):
        status, msg, request = reference_count(request_id)
        return status, msg, request

    def responseChecker(self, req_ids: List):
        all_ok_count = 0
        total_count = len(req_ids)
        while True:
            print("all count: "+str(all_ok_count)+", total count: "+str(total_count))
            self.orchestrator_state()
            if all_ok_count == total_count:
                return True
            all_ok_count = 0
            for val in req_ids:
                request_map = RequestMap.objects.filter(request_id=val).first()
                if request_map is not None:
                    all_ok_count += 1
                else:
                    time.sleep(3)

    def orchestrator_state(self):
        test = Tests.objects.filter(run_id=self.run_id).first()
        if test is None:
            raise Exception("Orchestrator removed")