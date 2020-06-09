# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of this
# software and associated documentation files (the "Software"), to deal in the Software
# without restriction, including without limitation the rights to use, copy, modify,
# merge, publish, distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
# INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A
# PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
# HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
# SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

import os
import io
import re
import json
import time
import boto3
import ntpath
import zipfile
import cfnresponse

def lambda_handler(event, context):
    if event['RequestType'] in ['Create', 'Update']:
        try:
            os.chdir('/tmp')
            filename = event['ResourceProperties'].get('Filename', 'lex.zip')
            s3 = event['ResourceProperties'].get('S3Bucket')
            lambdaname = event['ResourceProperties'].get('LambdaFunctionName')
            botname = event['ResourceProperties'].get('BotName')
            botalias = event['ResourceProperties'].get('BotAlias')
            loggroup = event['ResourceProperties'].get('LogGroup')
            logrole = event['ResourceProperties'].get('LogRole')
            exportname = '{}_Export.json'.format(botname)
            region = context.invoked_function_arn.split(":")[3]
            account = context.invoked_function_arn.split(":")[4]
        
            boto3.client('s3').download_file(s3, filename, filename)
            lex_client = boto3.client('lex-models')
        
            zipfile.ZipFile(filename).extractall()
            with open(exportname, 'r+') as file:
                content = file.read()
                file.seek(0)
                replaced = re.sub('arn:aws:lambda:(\w+)-(\w+)-(\d+):(\d+):function', 'arn:aws:lambda:\\1-\\2-\\3:{}:function'.format(account), content)
            
                buff = io.BytesIO()
                archive = zipfile.ZipFile(buff, mode='w')
                archive.writestr(ntpath.basename(filename), replaced)
                buff.seek(0)
                
                schema = json.loads(replaced)
                
                try:
                    boto3.client('lambda').add_permission(
                        FunctionName=lambdaname,
                        StatementId="{}-intents".format(botname),
                        Action="lambda:invokeFunction",
                        Principal="lex.amazonaws.com",
                        SourceArn="arn:aws:lex:{}:{}:intent:*".format(region, account)
                    )
                    print('Added permissions to {} function'.format(lambdaname))
                    
                except Exception as ex:
                    print('No permissions added: {}'.format(ex))
                    
                start_import_response = lex_client.start_import(
                    payload=buff.read(),
                    resourceType='BOT',
                    mergeStrategy='OVERWRITE_LATEST'
                )
                
                wait_for_status(lex_client.get_import, 'importStatus', ["IN_PROGRESS"], ["FAILED"], importId=start_import_response['importId'])
        
                bot_intents = []
                for intent in schema['resource']['intents']:
                    try:
                        intent_name = intent['name']
                        get_intent_response = lex_client.get_intent(
                            name=intent_name,
                            version='$LATEST'
                        )
                        create_intent_version_response = lex_client.create_intent_version(
                            checksum=get_intent_response['checksum'],
                            name=intent_name
                        )
                        bot_intents.append({
                            'intentName': intent_name,
                            'intentVersion': create_intent_version_response['version']
                        })
                    except Exception as ex:
                        print('No intent versions were added: {}'.format(ex))
                
                response = wait_for_status(lex_client.get_bot, 'status', ['BUILDING'], ["FAILED"], name=botname, versionOrAlias='$LATEST')
                
                lex_client.put_bot(
                    name=botname,
                    processBehavior='BUILD',
                    locale=schema['resource']['locale'],
                    childDirected=schema['resource']['childDirected'],
                    abortStatement=schema['resource']['abortStatement'],
                    checksum=response['checksum'],
                    clarificationPrompt=schema['resource']['clarificationPrompt'],
                    voiceId=schema['resource']['voiceId'],
                    idleSessionTTLInSeconds=schema['resource']['idleSessionTTLInSeconds'],
                    intents=bot_intents
                )
            
                response = wait_for_status(lex_client.get_bot, 'status', ['BUILDING', 'NOT_BUILT', 'READY_BASIC_TESTING'], ["FAILED"], name=botname, versionOrAlias='$LATEST')
                                      
                create_bot_version_response = lex_client.create_bot_version(
                        checksum=response['checksum'],
                        name=botname
                    )
                new_version = create_bot_version_response['version']
                
                try:
                    bot_alias = lex_client.get_bot_alias(
                            name=botalias,
                            botName=botname
                        )
                    checksum = bot_alias['checksum']
                    old_version = bot_alias['botVersion']
                    print('Updating alias {} with version {} and {}, {}'.format(botalias, new_version, loggroup, logrole))
                    put_bot_alias_response = lex_client.put_bot_alias(
                        name=botalias,
                        description='Update alias',
                        botVersion=new_version,
                        botName=botname,
                        checksum=checksum,
                        conversationLogs={
                            'logSettings': [
                                {
                                    'logType': 'TEXT',
                                    'destination': 'CLOUDWATCH_LOGS',
                                    'resourceArn': loggroup
                                },
                            ],
                            'iamRoleArn': logrole
                        }
                        )
                    print(put_bot_alias_response)
                    wait_for_status(lex_client.get_bot, 'status', ['BUILDING', 'NOT_BUILT', 'READY_BASIC_TESTING'], ["FAILED"], name=botname, versionOrAlias=botalias)
                    
                except Exception as ex:
                    print('Publishing alias {} with version {} and {} logging with {}'.format(botalias, new_version, loggroup, logrole))
                    put_bot_alias_response = lex_client.put_bot_alias(
                        name=botalias,
                        description='New alias',
                        botVersion=new_version,
                        botName=botname,
                        conversationLogs={
                            'logSettings': [
                                {
                                    'logType': 'TEXT',
                                    'destination': 'CLOUDWATCH_LOGS',
                                    'resourceArn': loggroup
                                },
                            ],
                            'iamRoleArn': logrole
                        }
                        )
                    print(put_bot_alias_response)
                    wait_for_status(lex_client.get_bot, 'status', ['BUILDING', 'NOT_BUILT', 'READY_BASIC_TESTING'], ["FAILED"], name=botname, versionOrAlias=botalias)
                    
                cfnresponse.send(event, context, cfnresponse.SUCCESS, {'Result': 'Bot has been successfully {}ed'.format(event['RequestType'])})

        except Exception as e:
            cfnresponse.send(event, context, cfnresponse.FAILED, {'Result': 'Failed to {} bot: {}'.format(event['RequestType'], e)})

    else:
        cfnresponse.send(event, context, cfnresponse.SUCCESS, {'Result': 'Operation {} bot did nothing'.format(event['RequestType'])})
 
def wait_for_status(f, status_name, status_values, failed_statuses=None, **kwargs):

    if failed_statuses is None:
        failed_statuses = []
    
    wait_response = {}
    
    for i in range(1, 20):
        try:
            wait_response = f(**kwargs)
            
            if wait_response[status_name] in status_values:
                print("Waiting for {} to exit from {} state".format(status_name, wait_response[status_name]))
            else:
                if wait_response[status_name] in failed_statuses:
                    print("No need to wait {} is {}\n".format(status_name, wait_response[status_name]))
                else:
                    print("Exiting with {} in {} state\n".format(status_name, wait_response[status_name]))
                return wait_response
            time.sleep(i)
        except Exception as e:
            print(e)
    
    return wait_response